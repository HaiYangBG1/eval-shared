"""
A/B 评估的 Dataset Run 缓存查询。

复用历史评估结果而不重跑：cache key 完全相同时（同 prompt+version+judge+role），
查到历史 run 中 item 的 trace_id，新 run 的 run-item 直接复用该 trace_id。
Langfuse 会通过 trace 级联自动聚合 score 到新 run，无需复制 score。

设计要点：
- Langfuse REST API 不支持按 metadata 过滤 runs，所以 cache key 必须编码进 runName，
  通过前缀过滤拿到候选 run，再 GET 单 run 拿 items 求交集。
- 多个历史 run 命中同一 item 时取最新（按 createdAt 倒序）。
- 命中后只复用 trace_id，不复制 score，避免污染历史 trace。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from eval_shared.common.langfuse_client import LangfuseClient

CacheRole = Literal["baseline", "candidate"]


@dataclass(frozen=True)
class CacheKey:
    """缓存键。完全相同的 key 之间可复用历史评估结果。"""

    prompt_name: str
    prompt_version: int
    judge_model: str
    role: CacheRole = "baseline"


@dataclass
class CacheLookupResult:
    hits: dict[str, str]               # dataset_item_id → trace_id（来自历史 run）
    miss_item_ids: list[str]
    source_run_names: list[str]        # 命中来源的 run 名（写入新 run metadata 用于审计）

    @property
    def hit_count(self) -> int:
        return len(self.hits)

    @property
    def miss_count(self) -> int:
        return len(self.miss_item_ids)


_RUN_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(value: str) -> str:
    """非法字符替换为 `-`，保证 runName 在 URL 路径中安全。"""
    return _RUN_NAME_SAFE.sub("-", value)


def build_run_name_prefix(key: CacheKey) -> str:
    """构造命中查询用的 runName 前缀（不含时间戳）。"""
    return (
        f"ab-{key.role}__{_sanitize(key.prompt_name)}__v{key.prompt_version}"
        f"__judge-{_sanitize(key.judge_model)}__"
    )


def build_run_name(key: CacheKey, *, ts: datetime | None = None) -> str:
    """构造完整 run 名称（含时间戳）。"""
    if ts is None:
        ts = datetime.now(timezone.utc)
    return build_run_name_prefix(key) + ts.strftime("%Y%m%dT%H%M%SZ")


def lookup_cache(
    client: LangfuseClient,
    *,
    dataset_name: str,
    target_item_ids: list[str],
    cache_key: CacheKey,
) -> CacheLookupResult:
    """查询 cache key 对应的历史 run，返回命中/未命中清单。

    Args:
        dataset_name: 在哪个 dataset 下找历史 run（如 `intention-golden`）
        target_item_ids: 本次评估要跑的 item id 列表
        cache_key: 命中匹配的键

    Returns:
        CacheLookupResult：hits 是命中 item → 历史 trace_id 的映射；
        miss_item_ids 必须实跑；source_run_names 用于审计写到新 run.metadata。
    """
    if not target_item_ids:
        return CacheLookupResult(hits={}, miss_item_ids=[], source_run_names=[])

    targets = set(target_item_ids)
    hits: dict[str, str] = {}
    source_run_names: list[str] = []

    prefix = build_run_name_prefix(cache_key)
    all_runs = client.list_dataset_runs(dataset_name)
    candidates = [r for r in all_runs if str(r.get("name", "")).startswith(prefix)]
    # Langfuse 未明确文档化排序，客户端按 createdAt 倒序确保"最新优先"
    candidates.sort(key=lambda r: r.get("createdAt") or "", reverse=True)

    for run in candidates:
        if not targets:
            break  # 全部命中，提前终止

        run_name = run.get("name", "")
        try:
            run_detail = client.get_dataset_run(dataset_name, run_name)
        except Exception:
            # 单 run 取不到（如被并发删除）不影响整体，跳过
            continue

        run_contributed = False
        for item in run_detail.get("datasetRunItems", []):
            item_id = item.get("datasetItemId")
            trace_id = item.get("traceId")
            if item_id and trace_id and item_id in targets:
                hits[item_id] = trace_id
                targets.remove(item_id)
                run_contributed = True

        if run_contributed:
            source_run_names.append(run_name)

    miss = [iid for iid in target_item_ids if iid in targets]
    return CacheLookupResult(
        hits=hits,
        miss_item_ids=miss,
        source_run_names=source_run_names,
    )


# 约定：promptfoo_ab 写入 Langfuse 的 score name
PROMPTFOO_PASS_SCORE_NAME = "promptfoo_pass"


def fetch_scores_by_trace_id(
    client: LangfuseClient,
    *,
    dataset_name: str,
    source_run_names: list[str],
    score_name: str = PROMPTFOO_PASS_SCORE_NAME,
) -> dict[str, float]:
    """从历史 dataset run 拉 score，建立 trace_id → numeric value 的索引。

    用于 cache 命中场景：promptfoo_ab 本地需要每条 hit case 当时 pass/fail
    才能算 stats/regressions。Langfuse UI 端通过 trace 级联会自动聚合，
    但本地计算必须主动拉。

    实现：按 datasetRunId 一次拉一个历史 run 的全部 scores，比逐 trace 查更高效。

    Args:
        dataset_name: 历史 run 所在 dataset
        source_run_names: lookup_cache 返回的命中来源 run 名清单
        score_name: 只关心这个 name 的 score（默认 promptfoo_pass）

    Returns:
        {trace_id: value}。同 trace_id 多次出现取最后一个（理论上唯一）。
    """
    by_trace: dict[str, float] = {}
    for run_name in source_run_names:
        try:
            run = client.get_dataset_run(dataset_name, run_name)
            run_id = run.get("id")
            if not run_id:
                continue
            run_trace_ids = [
                item.get("traceId")
                for item in run.get("datasetRunItems", [])
                if item.get("traceId")
            ]
            scores = client.list_scores(dataset_run_id=run_id, name=score_name)
            for s in scores:
                tid = s.get("traceId")
                value = s.get("value")
                if tid and isinstance(value, (int, float)):
                    by_trace[tid] = float(value)
            # Langfuse 不总是把复用 trace 的 score 暴露在 datasetRunId 查询下；
            # 对缺失项回退到 traceId 精确查询，保证本地 stats 不把 cache hit 误判为 fail。
            for tid in run_trace_ids:
                if tid in by_trace:
                    continue
                for s in client.list_scores(trace_id=tid, name=score_name):
                    value = s.get("value")
                    if isinstance(value, (int, float)):
                        by_trace[tid] = float(value)
                        break
        except Exception:
            # 单 run 取不到不影响整体（可能被并发删除），跳过
            continue
    return by_trace
