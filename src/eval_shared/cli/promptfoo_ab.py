"""
eval-promptfoo-ab — PromptFoo A/B 对比测试。

比较两个 Langfuse Prompt 版本（默认 production vs staging）
在同一测试集上的表现差异。

独立于 DSPy，适用于任何 Prompt 变更的回归检测。

用法：
  eval-promptfoo-ab --agent <name> [--baseline-label production] [--candidate-label staging]
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.ab_verdict import ABVerdict, compute_verdict
from eval_shared.common.config import init_env
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.dataset_run_cache import (
    PROMPTFOO_PASS_SCORE_NAME,
    CacheKey,
    CacheLookupResult,
    build_run_name,
    fetch_scores_by_trace_id,
    lookup_cache,
)
from eval_shared.common.ingestion import (
    build_score_event,
    build_trace_event,
    new_trace_id,
)
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.promptfoo_subset import (
    cleanup_subset_eval_files,
    filter_dataset_to_miss_subset,
    write_subset_eval_files,
)
from eval_shared.common.yaml_utils import dump_yaml, load_yaml
# 注：sync_dataset 的 _pull 在 --sync-dataset 选项启用时复用
# 放在函数体内 import 避免循环依赖（sync_dataset 不 import promptfoo_ab，目前没循环，
# 但模块顶部 import 有副作用风险）


# ── 工具函数 ──


def _pull_prompt_to(
    client: LangfuseClient, agent: str, label: str, dest: Path
) -> int:
    """拉指定 label 的 chat prompt 写到 dest，返回 prompt 版本号。

    版本号用于 A/B cache key（区分不同 prompt 版本的评估结果）。
    """
    prompt_name = f"{agent}-prompt"
    click.echo(f"  📥 拉取 {label} Prompt → {dest}")
    data = client.get_prompt(prompt_name, label=label)
    if data.get("type") != "chat":
        raise click.ClickException(
            f"暂不支持的 prompt 类型：{data.get('type')}（仅支持 chat）"
        )
    messages = data.get("prompt")
    if not isinstance(messages, list):
        raise click.ClickException("Langfuse 返回 prompt 字段非数组（chat 应为消息数组）")
    version = data.get("version")
    if not isinstance(version, int):
        raise click.ClickException(
            f"Langfuse 返回的 {label} prompt 缺少 version（cache 无法对齐版本）"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(messages, dest)
    return version


def _run_promptfoo(config_path: Path, output_path: str, prompt_path: Path) -> None:
    """运行 PromptFoo 评估，通过 -p 覆盖 config 中的 prompts，避免改动 prompt.yaml。

    使用实时输出模式，且容忍 telemetry 超时导致的非零退出码
    （PromptFoo 的 telemetry.shutdown() 在子进程中可能超时返回 exit code 100，
    但评估结果已正确写入文件）。
    """
    abs_prompt = prompt_path.resolve()
    cmd = [
        "npx", "promptfoo", "eval",
        "-c", str(config_path),
        "-o", output_path,
        "-p", f"file://{abs_prompt}",
        "--no-cache",
    ]
    click.echo(f"  🔄 PromptFoo 评估 → {output_path}")
    click.echo(f"     $ {' '.join(cmd)}")
    result = subprocess.run(cmd)

    output_exists = Path(output_path).exists()
    if result.returncode != 0 and not output_exists:
        raise click.ClickException(
            f"PromptFoo 评估失败 (exit code {result.returncode})，且未生成结果文件"
        )
    if result.returncode != 0 and output_exists:
        click.echo(
            f"  ⚠️ PromptFoo 退出码 {result.returncode}（结果文件已生成，忽略 telemetry 超时）"
        )
    click.echo("  ✅ PromptFoo 评估完成")


def _run_promptfoo_subset(
    *,
    agent: str,
    full_dataset: list,
    dataset_name: str,
    miss_item_ids: list[str],
    business_config_path: Path,
    prompt_path: Path,
    output_path: str,
    tmp_basename: str,
) -> list[dict]:
    """跑 PromptFoo 但只评估 miss 子集；hit 不跑（由调用方从历史 score 复用）。

    临时 dataset/config 写在 agents/<agent>/. 前缀文件，跑完清理。
    miss_item_ids 为空时直接返回 []，不调 PromptFoo。
    """
    if not miss_item_ids:
        click.echo(f"  ✅ 全部命中缓存，跳过 PromptFoo 实跑（{tmp_basename}）")
        return []

    subset = filter_dataset_to_miss_subset(
        full_dataset,
        dataset_name=dataset_name,
        miss_item_ids=set(miss_item_ids),
    )
    if not subset:
        click.echo(
            "  ⚠️ miss 列表非空但本地 dataset YAML 中找不到对应 case（id 算法变了？）"
        )
        return []

    tmp_config, tmp_dataset = write_subset_eval_files(
        agent=agent,
        dataset_subset=subset,
        business_config_path=business_config_path,
        tmp_basename=tmp_basename,
    )
    try:
        click.echo(f"  📌 子集跑：{len(subset)}/{len(full_dataset)} 条 miss")
        _run_promptfoo(tmp_config, output_path, prompt_path)
        return load_results(output_path)
    finally:
        cleanup_subset_eval_files(tmp_config, tmp_dataset)


def _judge_model_id() -> str:
    """读评分模型标识作为 cache key 一部分。未设置时回退 'default'。"""
    return os.environ.get("PROMPTFOO_GRADING_MODEL", "default")


def _infer_local_dataset_path(agent: str, dataset_name: str) -> Path:
    """从 Langfuse dataset_name 推断本地 YAML 路径。

    约定：`{agent}-{type}` → `agents/{agent}/datasets/{type}.yaml`。
    不符合 `{agent}-` 前缀的（自定义 dataset 名）直接拿原名当文件名。
    """
    prefix = f"{agent}-"
    type_ = dataset_name[len(prefix):] if dataset_name.startswith(prefix) else dataset_name
    return Path(f"agents/{agent}/datasets/{type_}.yaml")


def _vars_key(vars_data: dict) -> str:
    return json.dumps(vars_data, sort_keys=True, ensure_ascii=False)


def _merge_hit_and_miss(
    *,
    full_dataset: list,
    dataset_name: str,
    hits: dict[str, str],
    hit_scores: dict[str, float],
    miss_results: list[dict],
) -> tuple[list[dict], dict[str, str]]:
    """合并 PromptFoo miss 跑出的结果 + cache 命中的历史 score → 完整结果列表。

    返回 (results, item_id_to_trace_id)：
      - results：给 calc_stats 用（保持 full_dataset 顺序）
      - item_id_to_trace_id：仅含 hit case；miss 的 trace_id 由 _write_langfuse_run
        生成新 UUID 后回填到同一个 dict

    score 阈值 >= 0.5 视为 pass（约定 1.0=pass / 0.0=fail）。
    """
    miss_by_vars: dict[str, dict] = {
        _vars_key(r.get("vars", {})): r for r in miss_results
    }

    results: list[dict] = []
    item_id_to_trace: dict[str, str] = {}

    for case in full_dataset:
        if not isinstance(case, dict):
            continue
        vars_data = case.get("vars", {})
        item_id = compute_item_id(dataset_name, vars_data)

        if item_id in hits:
            trace_id = hits[item_id]
            score = hit_scores.get(trace_id, 0.0)
            results.append({
                "vars": vars_data,
                "success": score >= 0.5,
                "_cache_hit": True,
            })
            item_id_to_trace[item_id] = trace_id
        else:
            r = miss_by_vars.get(_vars_key(vars_data))
            if r is None:
                results.append({"vars": vars_data, "success": False, "_cache_hit": False})
            else:
                results.append({**r, "_cache_hit": False})

    return results, item_id_to_trace


def _downgrade_scoreless_hits_to_miss(
    cache_result: CacheLookupResult,
    hit_scores: dict[str, float],
    *,
    role: str,
) -> CacheLookupResult:
    """把缺少 score 的 cache hit 降级成 miss，避免本地统计误判。

    命中 cache 只说明能复用 traceId；如果 score 还没被 Langfuse 索引到，
    本轮必须重跑这些 case，否则 `_merge_hit_and_miss` 会保守按 fail 处理，
    造成通过率被大量低估。
    """
    scoreless = [
        item_id
        for item_id, trace_id in cache_result.hits.items()
        if trace_id not in hit_scores
    ]
    if not scoreless:
        return cache_result

    click.echo(
        f"  ⚠️ {role} cache hit 中 {len(scoreless)} 条缺少 score，降级为 miss 重跑"
    )
    return CacheLookupResult(
        hits={
            item_id: trace_id
            for item_id, trace_id in cache_result.hits.items()
            if item_id not in set(scoreless)
        },
        miss_item_ids=[*cache_result.miss_item_ids, *scoreless],
        source_run_names=cache_result.source_run_names,
    )


def _build_run_metadata(
    *,
    role: str,
    prompt_name: str,
    prompt_version: int,
    prompt_label: str,
    judge_model: str,
    stats: dict,
    cache_result: CacheLookupResult,
    extra: dict | None = None,
) -> dict:
    """构造 dataset run.metadata。

    ⚠️ Langfuse run.metadata 是 first-write-wins（4d.5 spike 验证），必须在
    第一个 run-item POST 时一次性传完，不能后续追加。
    """
    md = {
        "role": role,
        "prompt_name": prompt_name,
        "prompt_version": prompt_version,
        "prompt_label": prompt_label,
        "judge_model": judge_model,
        "pass_count": stats["pass"],
        "fail_count": stats["fail"],
        "total": stats["total"],
        "rate": float(stats["rate"]),
        "cached_count": cache_result.hit_count,
        "executed_count": cache_result.miss_count,
        "cache_source_run_names": list(cache_result.source_run_names),
    }
    if extra:
        md.update(extra)
    return md


def _write_langfuse_run(
    *,
    client: LangfuseClient,
    dataset_name: str,
    run_name: str,
    full_dataset: list,
    cache_result: CacheLookupResult,
    miss_results: list[dict],
    item_id_to_trace: dict[str, str],
    run_metadata: dict,
    role: str,
) -> None:
    """写一个 Dataset Run：先 ingestion (miss 的 trace + score)，再 run-items。

    流程：
      1. 给每个 miss case 生成新 UUID 作为 trace_id（写入 item_id_to_trace 共享映射）
      2. 构造 ingestion batch（trace-create + score-create）→ POST
      3. POST 第一个 run-item 携带完整 run.metadata（first-write-wins 必须一次性）
      4. POST 剩余 run-items（不带 metadata）

    cache 命中的 case 不发 ingestion——直接复用 hits 里的历史 trace_id。
    """
    miss_set = set(cache_result.miss_item_ids)
    miss_by_vars: dict[str, dict] = {
        _vars_key(r.get("vars", {})): r for r in miss_results
    }

    # Step 1+2: miss case 生成新 trace_id + 构造 ingestion
    ingestion_events: list[dict] = []
    for case in full_dataset:
        if not isinstance(case, dict):
            continue
        vars_data = case.get("vars", {})
        item_id = compute_item_id(dataset_name, vars_data)
        if item_id not in miss_set:
            continue

        trace_id = new_trace_id()
        item_id_to_trace[item_id] = trace_id

        result = miss_by_vars.get(_vars_key(vars_data), {})
        success = bool(result.get("success", result.get("pass", False)))
        response_obj = result.get("response") or {}
        output_value = (
            response_obj.get("output") if isinstance(response_obj, dict) else None
        )
        if output_value is None:
            output_value = result.get("output", "")

        ingestion_events.append(build_trace_event(
            trace_id=trace_id,
            name=f"{role}__{run_name[:60]}",
            input_data=vars_data,
            output=output_value,
            metadata={"item_id": item_id, "role": role},
        ))
        ingestion_events.append(build_score_event(
            trace_id=trace_id,
            name=PROMPTFOO_PASS_SCORE_NAME,
            value=1.0 if success else 0.0,
        ))

    if ingestion_events:
        click.echo(f"  📤 ingestion batch: {len(ingestion_events)} events")
        try:
            client.submit_ingestion_batch(ingestion_events)
        except Exception as e:
            click.echo(f"  ⚠️ ingestion 失败（不影响 run-item 写入）: {e}")

    # Step 3+4: POST run-items（第一个带 metadata，其余不带）
    first_metadata: dict | None = run_metadata
    posted = 0
    for case in full_dataset:
        if not isinstance(case, dict):
            continue
        vars_data = case.get("vars", {})
        item_id = compute_item_id(dataset_name, vars_data)
        trace_id = item_id_to_trace.get(item_id)
        if not trace_id:
            continue
        try:
            client.create_dataset_run_item(
                run_name=run_name,
                dataset_item_id=item_id,
                trace_id=trace_id,
                metadata=first_metadata,
                run_description=(
                    f"A/B {role} for "
                    f"{run_metadata.get('prompt_name')} v{run_metadata.get('prompt_version')}"
                    if first_metadata else None
                ),
            )
            posted += 1
            first_metadata = None  # 仅第一个 item 带 metadata
        except Exception as e:
            click.echo(f"  ⚠️ run-item 写入失败 (item={item_id[:16]}...): {e}")

    click.echo(f"  ✅ Dataset Run: {run_name}（{posted}/{len(full_dataset)} run-items）")


# ── 结果分析 ──


def load_results(file_path: str) -> list[dict]:
    """加载 PromptFoo 结果 JSON。"""
    p = Path(file_path)
    if not p.exists():
        raise click.ClickException(f"结果文件不存在: {file_path}")
    raw = json.loads(p.read_text("utf-8"))
    results_obj = raw.get("results", raw)
    return results_obj if isinstance(results_obj, list) else results_obj.get("results", [])


def calc_stats(results: list[dict]) -> dict:
    """统计通过/失败数量和通过率。"""
    total = pass_count = fail_count = 0
    for r in results:
        total += 1
        success = r.get("success", r.get("pass"))
        if success is True:
            pass_count += 1
        elif success is False:
            fail_count += 1
    rate = f"{(pass_count / total * 100):.1f}" if total > 0 else "0.0"
    return {"total": total, "pass": pass_count, "fail": fail_count, "rate": rate}


def find_regressions_and_improvements(
    base_results: list[dict], cand_results: list[dict]
) -> tuple[list[dict], list[dict]]:
    """找出回归和改善的用例。"""

    def _key(r: dict) -> str:
        return json.dumps(r.get("vars", {}), sort_keys=True)

    base_map = {_key(r): r for r in base_results}
    regressions: list[dict] = []
    improvements: list[dict] = []

    for r in cand_results:
        key = _key(r)
        base = base_map.get(key)
        if base is None:
            continue
        cand_pass = r.get("success", r.get("pass")) is True
        base_pass = base.get("success", base.get("pass")) is True

        if base_pass and not cand_pass:
            regressions.append({
                "vars": r.get("vars", {}),
                "base_output": (
                    base.get("response", {}).get("output") or base.get("output", "")
                )[:200],
                "cand_output": (
                    r.get("response", {}).get("output") or r.get("output", "")
                )[:200],
            })
        elif not base_pass and cand_pass:
            improvements.append({"vars": r.get("vars", {})})

    return regressions, improvements


def is_safe_to_upgrade(
    base_stats: dict,
    cand_stats: dict,
    regressions: list[dict],
    improvements: list[dict],
    tolerance: float = 0.0,
) -> bool:
    """升级门禁：等价于 `compute_verdict == BETTER`（无回归、有改善、提升 > tolerance）。

    保留作为简单布尔接口（旧调用方 / 报告中"是否安全升级"判断），
    内部委托 `compute_verdict`，保证报告结论与三态 verdict / Langfuse label 永远一致。
    """
    rate_diff = float(cand_stats["rate"]) - float(base_stats["rate"])
    verdict = compute_verdict(
        rate_diff=rate_diff,
        regressions=len(regressions),
        improvements=len(improvements),
        tolerance=tolerance,
    )
    return verdict == ABVerdict.BETTER


# ── 报告生成 ──


def generate_ab_report(
    agent: str,
    base_label: str,
    cand_label: str,
    base_stats: dict,
    cand_stats: dict,
    regressions: list[dict],
    improvements: list[dict],
    baseline_path: str,
    candidate_path: str,
    tolerance: float = 0.0,
) -> str:
    """生成 A/B 对比报告（Markdown）。"""
    rate_diff = float(cand_stats["rate"]) - float(base_stats["rate"])
    rate_emoji = "📈" if rate_diff > 0 else ("📉" if rate_diff < 0 else "➡️")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    safe = is_safe_to_upgrade(
        base_stats, cand_stats, regressions, improvements, tolerance=tolerance
    )

    lines = [
        f"# 📊 PromptFoo A/B 对比报告 — {agent}",
        "",
        f"> 生成时间：{now}",
        f"> Baseline：`{base_label}` (`{baseline_path}`)",
        f"> Candidate：`{cand_label}` (`{candidate_path}`)",
        "",
        "## 总体对比",
        "",
        f"| 指标 | {base_label} | {cand_label} | 变化 |",
        "|------|----------|-----------|------|",
        f"| 总测试数 | {base_stats['total']} | {cand_stats['total']} | — |",
        f"| ✅ 通过 | {base_stats['pass']} | {cand_stats['pass']} | {cand_stats['pass'] - base_stats['pass']:+d} |",
        f"| ❌ 失败 | {base_stats['fail']} | {cand_stats['fail']} | {cand_stats['fail'] - base_stats['fail']:+d} |",
        f"| **通过率** | **{base_stats['rate']}%** | **{cand_stats['rate']}%** | **{rate_emoji} {rate_diff:+.1f}%** |",
        "",
    ]

    if regressions:
        lines.extend([
            f"## 🔴 回归 ({len(regressions)} 个用例)",
            "",
            "> 以下用例在 Baseline 中通过，但在 Candidate 中失败：",
            "",
        ])
        for i, r in enumerate(regressions[:5], 1):
            lines.extend([
                f"### {i}. 回归用例",
                "",
                "**输入：**",
                "```json",
                json.dumps(r["vars"], ensure_ascii=False, indent=2)[:300],
                "```",
                f"**{base_label} 输出：** {r['base_output']}",
                "",
                f"**{cand_label} 输出：** {r['cand_output']}",
                "",
                "---",
                "",
            ])
        if len(regressions) > 5:
            lines.append(f"> ℹ️ 还有 {len(regressions) - 5} 个回归用例未列出。")
            lines.append("")

    if improvements:
        lines.extend([
            f"## 🟢 改善 ({len(improvements)} 个用例)",
            "",
            "> 以下用例在 Baseline 中失败，但在 Candidate 中通过：",
            "",
        ])
        for imp in improvements[:5]:
            lines.append(f"- {json.dumps(imp['vars'], ensure_ascii=False)[:200]}")
        if len(improvements) > 5:
            lines.append(f"- ... 还有 {len(improvements) - 5} 个")
        lines.append("")

    # 结论
    lines.extend(["## 结论", ""])
    if safe:
        lines.append(
            f"✅ **安全升级**：无回归，改善 {len(improvements)} 个，"
            "且通过率未下降。建议执行 "
            f"`npm run promote -- --agent {agent}`。"
        )
    elif regressions:
        lines.append(
            f"⚠️ **存在回归**：{len(regressions)} 个用例从 PASS → FAIL，建议排查后再决定。"
        )
    elif rate_diff >= -tolerance:
        lines.append(
            "➡️ **无明显改善**：未发现回归，但无改善用例或变化未超过容忍阈值，不建议自动升级。"
        )
    else:
        lines.append(
            f"📉 **通过率下降 {rate_diff:+.1f}%**，无可匹配的回归用例，可能是新增测试导致。"
        )
    lines.append("")
    return "\n".join(lines)


# ── CLI 入口 ──


@click.command()
@click.option("--agent", required=True, help="Agent 名称（如 intention）")
@click.option("--baseline-label", default="production", help="Baseline Prompt 标签（默认 production）")
@click.option("--candidate-label", default="staging", help="Candidate Prompt 标签（默认 staging）")
@click.option("--output", "output_dir", default="output", help="输出目录（默认 output/）")
@click.option(
    "--tolerance",
    type=float,
    default=1.0,
    help="通过率变化容忍阈值（百分比，默认 1.0%）。±tolerance 内视为 SAME (🟰)。",
)
@click.option(
    "--dataset",
    "dataset_name_arg",
    default=None,
    help="Langfuse Dataset 名（默认 {agent}-golden）。本地 YAML 自动从约定 datasets/{type}.yaml 推断。",
)
@click.option(
    "--sync-dataset",
    is_flag=True,
    help="跑前先从 Langfuse 拉最新 dataset 覆盖本地 YAML（确保用最新评估集）。默认关，向后兼容；CI 推荐开",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="禁用 Langfuse Dataset Run 缓存复用，全部 case 实跑 PromptFoo（CI 门禁建议开）",
)
@click.option("--dry-run", is_flag=True, help="只检查环境，不执行评估")
def main(
    agent: str,
    baseline_label: str,
    candidate_label: str,
    output_dir: str,
    tolerance: float,
    dataset_name_arg: str | None,
    sync_dataset: bool,
    no_cache: bool,
    dry_run: bool,
):
    """PromptFoo A/B 对比测试 — 比较两个 Prompt 版本的评估结果。"""
    init_env()

    click.echo("")
    click.echo("╔══════════════════════════════════════════════════╗")
    click.echo("║       eval-promptfoo-ab · A/B 对比测试            ║")
    click.echo("╚══════════════════════════════════════════════════╝")
    click.echo(f"  Agent     : {agent}")
    click.echo(f"  Dataset   : {dataset_name_arg or f'{agent}-golden (default)'}")
    click.echo(f"  Baseline  : {baseline_label}")
    click.echo(f"  Candidate : {candidate_label}")
    click.echo(f"  Tolerance : ±{tolerance:.1f}%")
    click.echo(f"  Cache     : {'❌ disabled (--no-cache)' if no_cache else '✅ enabled'}")
    click.echo(f"  Sync DS   : {'✅ pull from Langfuse before run' if sync_dataset else '⏭️  use local YAML (default)'}")
    click.echo(f"  模式      : {'🧪 DRY-RUN' if dry_run else '🚀 正式运行'}")
    click.echo("")

    # 检查前置条件
    business_config_path = Path(f"agents/{agent}/promptfooconfig.yaml")
    dataset_name = dataset_name_arg or f"{agent}-golden"
    local_dataset_path = _infer_local_dataset_path(agent, dataset_name)
    if not business_config_path.exists():
        raise click.ClickException(f"找不到 PromptFoo 配置: {business_config_path}")
    if not local_dataset_path.exists():
        raise click.ClickException(
            f"找不到本地 dataset: {local_dataset_path}（先 `eval-sync-dataset --agent {agent} --type {dataset_name.removeprefix(agent + '-') or 'golden'}` 拉取）"
        )

    click.echo("  ✅ 前置条件检查通过")

    if dry_run:
        click.echo("\n🧪 DRY-RUN 完成 — 环境检查通过。")
        return

    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)
    baseline_output = f"{output_dir}/{agent}-ab-baseline.json"
    candidate_output = f"{output_dir}/{agent}-ab-candidate.json"
    baseline_prompt = output_dir_path / f"{agent}-ab-baseline.prompt.yaml"
    candidate_prompt = output_dir_path / f"{agent}-ab-candidate.prompt.yaml"

    # dataset_name 在前置条件检查时已确定（默认 {agent}-golden，可被 --dataset 覆盖）
    prompt_name = f"{agent}-prompt"
    judge_model = _judge_model_id()

    baseline_run_name: str | None = None
    candidate_run_name: str | None = None

    with LangfuseClient() as client:
        # ━━━ Step 0 (optional): 从 Langfuse 拉最新 dataset 覆盖本地 YAML ━━━
        if sync_dataset:
            click.echo("\n━━━ Step 0/5: 从 Langfuse 同步最新 dataset ━━━")
            # 函数级 import 避免顶部循环引用风险
            from eval_shared.cli.sync_dataset import _pull as _sync_pull
            prefix = f"{agent}-"
            type_ = (
                dataset_name[len(prefix):]
                if dataset_name.startswith(prefix)
                else dataset_name
            )
            _sync_pull(client, agent, dataset_name, type_)

        # 读本地 dataset 全集，复算所有 item_ids（此时 yaml 已经是最新 / 或本地工作副本）
        full_dataset = load_yaml(local_dataset_path)
        if not isinstance(full_dataset, list):
            raise click.ClickException(
                f"dataset YAML 不是数组结构: {local_dataset_path}"
            )
        all_item_ids = [
            compute_item_id(dataset_name, c.get("vars", {}))
            for c in full_dataset
            if isinstance(c, dict)
        ]

        # ━━━ Step 1: 拉 prompts 拿版本号 ━━━
        click.echo("\n━━━ Step 1/5: 拉取 baseline + candidate prompt ━━━")
        baseline_version = _pull_prompt_to(client, agent, baseline_label, baseline_prompt)
        candidate_version = _pull_prompt_to(client, agent, candidate_label, candidate_prompt)

        # ━━━ Step 2: 缓存查询（baseline + candidate）━━━
        click.echo("\n━━━ Step 2/5: 缓存查询 ━━━")
        if no_cache:
            empty = CacheLookupResult(hits={}, miss_item_ids=list(all_item_ids), source_run_names=[])
            base_cache = empty
            cand_cache = CacheLookupResult(hits={}, miss_item_ids=list(all_item_ids), source_run_names=[])
            click.echo("  ❌ --no-cache：全部 case 实跑")
        else:
            base_cache = lookup_cache(
                client,
                dataset_name=dataset_name,
                target_item_ids=all_item_ids,
                cache_key=CacheKey(prompt_name, baseline_version, judge_model, "baseline"),
            )
            cand_cache = lookup_cache(
                client,
                dataset_name=dataset_name,
                target_item_ids=all_item_ids,
                cache_key=CacheKey(prompt_name, candidate_version, judge_model, "candidate"),
            )
            click.echo(
                f"  Baseline  : hit {base_cache.hit_count} / miss {base_cache.miss_count}"
            )
            click.echo(
                f"  Candidate : hit {cand_cache.hit_count} / miss {cand_cache.miss_count}"
            )

        # 拉 hit case 的历史 score（本地算 stats 用）
        base_hit_scores = fetch_scores_by_trace_id(
            client,
            dataset_name=dataset_name,
            source_run_names=base_cache.source_run_names,
        )
        cand_hit_scores = fetch_scores_by_trace_id(
            client,
            dataset_name=dataset_name,
            source_run_names=cand_cache.source_run_names,
        )

        base_cache = _downgrade_scoreless_hits_to_miss(
            base_cache, base_hit_scores, role="Baseline"
        )
        cand_cache = _downgrade_scoreless_hits_to_miss(
            cand_cache, cand_hit_scores, role="Candidate"
        )

        # ━━━ Step 3: 跑 PromptFoo subset（只跑 miss）━━━
        click.echo(f"\n━━━ Step 3/5: Baseline ({baseline_label}) miss-only 跑 ━━━")
        base_miss_results = _run_promptfoo_subset(
            agent=agent,
            full_dataset=full_dataset,
            dataset_name=dataset_name,
            miss_item_ids=base_cache.miss_item_ids,
            business_config_path=business_config_path,
            prompt_path=baseline_prompt,
            output_path=baseline_output,
            tmp_basename=f"promptfoo-ab-{agent}-baseline-miss",
        )

        click.echo(f"\n━━━ Step 4/5: Candidate ({candidate_label}) miss-only 跑 ━━━")
        cand_miss_results = _run_promptfoo_subset(
            agent=agent,
            full_dataset=full_dataset,
            dataset_name=dataset_name,
            miss_item_ids=cand_cache.miss_item_ids,
            business_config_path=business_config_path,
            prompt_path=candidate_prompt,
            output_path=candidate_output,
            tmp_basename=f"promptfoo-ab-{agent}-candidate-miss",
        )

        # ━━━ Step 5: 合并 hit + miss 结果 → 算 stats / verdict → 写 Langfuse ━━━
        click.echo("\n━━━ Step 5/5: 合并结果 + 写 Langfuse Dataset Run ━━━")

        base_results, base_item_to_trace = _merge_hit_and_miss(
            full_dataset=full_dataset,
            dataset_name=dataset_name,
            hits=base_cache.hits,
            hit_scores=base_hit_scores,
            miss_results=base_miss_results,
        )
        cand_results, cand_item_to_trace = _merge_hit_and_miss(
            full_dataset=full_dataset,
            dataset_name=dataset_name,
            hits=cand_cache.hits,
            hit_scores=cand_hit_scores,
            miss_results=cand_miss_results,
        )

        base_stats = calc_stats(base_results)
        cand_stats = calc_stats(cand_results)
        regressions, improvements = find_regressions_and_improvements(
            base_results, cand_results
        )
        rate_diff_value = float(cand_stats["rate"]) - float(base_stats["rate"])
        verdict_for_runs = compute_verdict(
            rate_diff=rate_diff_value,
            regressions=len(regressions),
            improvements=len(improvements),
            tolerance=tolerance,
        )

        # 写 baseline run
        baseline_run_name = build_run_name(
            CacheKey(prompt_name, baseline_version, judge_model, "baseline")
        )
        _write_langfuse_run(
            client=client,
            dataset_name=dataset_name,
            run_name=baseline_run_name,
            full_dataset=full_dataset,
            cache_result=base_cache,
            miss_results=base_miss_results,
            item_id_to_trace=base_item_to_trace,
            run_metadata=_build_run_metadata(
                role="baseline",
                prompt_name=prompt_name,
                prompt_version=baseline_version,
                prompt_label=baseline_label,
                judge_model=judge_model,
                stats=base_stats,
                cache_result=base_cache,
            ),
            role="baseline",
        )

        # 写 candidate run（metadata 含 verdict / regressions / 等评估结论）
        candidate_run_name = build_run_name(
            CacheKey(prompt_name, candidate_version, judge_model, "candidate")
        )
        _write_langfuse_run(
            client=client,
            dataset_name=dataset_name,
            run_name=candidate_run_name,
            full_dataset=full_dataset,
            cache_result=cand_cache,
            miss_results=cand_miss_results,
            item_id_to_trace=cand_item_to_trace,
            run_metadata=_build_run_metadata(
                role="candidate",
                prompt_name=prompt_name,
                prompt_version=candidate_version,
                prompt_label=candidate_label,
                judge_model=judge_model,
                stats=cand_stats,
                cache_result=cand_cache,
                extra={
                    "verdict": verdict_for_runs.value,
                    "regressions": len(regressions),
                    "improvements": len(improvements),
                    "rate_diff": rate_diff_value,
                    "tolerance": tolerance,
                    "baseline_run_name": baseline_run_name,
                },
            ),
            role="candidate",
        )

    report = generate_ab_report(
        agent, baseline_label, candidate_label,
        base_stats, cand_stats, regressions, improvements,
        baseline_output, candidate_output,
        tolerance=tolerance,
    )

    report_path = f"{output_dir}/{agent}-ab-report.md"
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(report, encoding="utf-8")

    verdict = verdict_for_runs  # 与 Langfuse run.metadata 写入的 verdict 一致

    # 同时输出机器可读的 JSON 摘要（供 pipeline 编排层读取）
    summary = {
        "agent": agent,
        "baseline_label": baseline_label,
        "candidate_label": candidate_label,
        "baseline": base_stats,
        "candidate": cand_stats,
        "regressions": len(regressions),
        "improvements": len(improvements),
        "rate_diff": rate_diff_value,
        "tolerance": tolerance,
        "verdict": verdict.value,
        "safe_to_upgrade": is_safe_to_upgrade(
            base_stats, cand_stats, regressions, improvements, tolerance=tolerance
        ),
        "cache": {
            "baseline_hits": base_cache.hit_count,
            "baseline_miss": base_cache.miss_count,
            "candidate_hits": cand_cache.hit_count,
            "candidate_miss": cand_cache.miss_count,
        },
        "langfuse_run_names": {
            "baseline": baseline_run_name,
            "candidate": candidate_run_name,
        },
    }
    summary_path = f"{output_dir}/{agent}-ab-summary.json"
    Path(summary_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 终端摘要
    rate_emoji = "📈" if rate_diff_value > 0 else ("📉" if rate_diff_value < 0 else "➡️")

    click.echo("")
    click.echo("╔══════════════════════════════════════╗")
    click.echo("║         A/B 对比结果                  ║")
    click.echo("╠══════════════════════════════════════╣")
    click.echo(f"║  {baseline_label:>10} : {base_stats['rate']:>5}% ({base_stats['pass']}/{base_stats['total']})    ║")
    click.echo(f"║  {candidate_label:>10} : {cand_stats['rate']:>5}% ({cand_stats['pass']}/{cand_stats['total']})    ║")
    click.echo(f"║  变化       : {rate_emoji} {rate_diff_value:+.1f}%               ║")
    if regressions:
        click.echo(f"║  🔴 回归    : {len(regressions)} 个                    ║")
    if improvements:
        click.echo(f"║  🟢 改善    : {len(improvements)} 个                    ║")
    click.echo("╚══════════════════════════════════════╝")
    click.echo(
        f"\n💾 缓存命中：baseline {base_cache.hit_count}/{len(all_item_ids)}，"
        f"candidate {cand_cache.hit_count}/{len(all_item_ids)}"
    )
    if baseline_run_name:
        click.echo(f"📦 Langfuse Dataset Run (baseline):  {baseline_run_name}")
    if candidate_run_name:
        click.echo(f"📦 Langfuse Dataset Run (candidate): {candidate_run_name}")
    click.echo(f"📊 对比报告 → {report_path}")

    if verdict == ABVerdict.BETTER:
        click.echo(
            f"\n✅ {verdict.value}（无回归，改善 {len(improvements)} 个，"
            f"通过率提升 {rate_diff_value:+.1f}% > 阈值 {tolerance:.1f}%）："
            f"建议执行 npm run promote -- --agent {agent}"
        )
    elif verdict == ABVerdict.WORSE:
        click.echo(
            f"\n❌ {verdict.value}：存在 {len(regressions)} 个回归 / 通过率 {rate_diff_value:+.1f}%，"
            "promote 会被门禁阻断（如确认是噪音可加 --force）"
        )
    else:
        click.echo(
            f"\n🟰 {verdict.value}：通过率变化 {rate_diff_value:+.1f}% 在 ±{tolerance:.1f}% 容忍区间内，"
            "或净改善不足，promote 不会被阻断但也无明显升级理由"
        )


if __name__ == "__main__":
    main()
