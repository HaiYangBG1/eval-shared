"""
eval-online — 拉取 Langfuse 线上 Observation → LLM-as-a-Judge 评估 → 写回 Score。

解决的问题：
  Dify 不支持 OTel 格式，Langfuse 内置的 Observation-level Evaluator 无法触发。
  本脚本用外部批处理方式实现等效功能。

用法：
  eval-online [--config <path>] [--hours <n>] [--limit <n>] [--dry-run] [--force] [--verbose]
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import click
import httpx

from eval_shared.common.config import init_env, get_langfuse_config, get_eval_model_config
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.yaml_utils import load_yaml


def _online_dataset_name(agent: str) -> str:
    return f"{agent}-online-temp"


def _online_run_name(agent: str, score_name: str, ts: datetime) -> str:
    """eval-online Dataset Run 命名约定。

    用 `online-` 前缀区分 A/B 的 `ab-baseline/ab-candidate` 前缀。
    """
    return f"online-{agent}-{score_name}-{ts.strftime('%Y%m%dT%H%M%SZ')}"


# ── 字段提取 ──

def _extract_field(obs: dict, jsonpath: str) -> Any:
    """简易 JSONPath 提取（支持 $.key、$[n]、$.key[n].subkey 等）。

    不支持：通配符 [*]、过滤器 ?(...)、递归下降 ..、脚本表达式
    遇到不支持的语法会抛出 ValueError。
    """
    if not jsonpath:
        return obs.get("input")
    for token in ("[*]", "?(", "..", "@."):
        if token in jsonpath:
            raise ValueError(
                f'jsonpath 含不支持的语法："{jsonpath}"（仅支持 $.key、$[n]、$.key[n].subkey）'
            )
    if jsonpath == "$.input":
        return obs.get("input")
    if jsonpath == "$.output":
        return obs.get("output")

    # 定位根对象
    if jsonpath.startswith("$.input"):
        root = obs.get("input")
        rest = jsonpath[len("$.input"):]
    elif jsonpath.startswith("$.output"):
        root = obs.get("output")
        rest = jsonpath[len("$.output"):]
    else:
        root = obs
        rest = jsonpath[1:]  # 去掉 $

    if not rest:
        return root

    # 解析路径段
    segments: list[str | int] = []
    for m in re.finditer(r"\.([a-zA-Z_][a-zA-Z0-9_]*)|\[(-?\d+)\]", rest):
        if m.group(1) is not None:
            segments.append(m.group(1))
        elif m.group(2) is not None:
            segments.append(int(m.group(2)))

    current = root
    for seg in segments:
        if current is None:
            return None
        if isinstance(seg, int):
            if not isinstance(current, list):
                return None
            idx = seg if seg >= 0 else len(current) + seg
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(seg)
    return current


def _stringify(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    import json
    return json.dumps(val, ensure_ascii=False)


def _truncate(s: str, length: int) -> str:
    if not s or len(s) <= length:
        return s or ""
    return s[:length] + "…"


# ── LLM Judge ──

def _call_judge(prompt: str, eval_config: dict) -> str:
    """调用 LLM 评分。"""
    url = f"{eval_config['base_url']}/chat/completions"
    # 修复双斜杠
    url = re.sub(r"([^:])/+", r"\1/", url)

    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {eval_config['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": eval_config["model_name"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 64,
        },
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    return (data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()


def _parse_score(text: str, score_type: str, allowed_values: list | None) -> float | None:
    """从 Judge 响应中解析分数。"""
    if not text:
        return None

    m = re.search(r"(?:^|[\s:：])(\d+(?:\.\d+)?)", text)
    if not m:
        return None

    num = float(m.group(1))

    if score_type == "BOOLEAN":
        return 1.0 if num >= 0.5 else 0.0

    if allowed_values:
        closest = min(allowed_values, key=lambda v: abs(num - v))
        return float(closest)

    return num


# ── 核心逻辑 ──

def _process_evaluator(
    evaluator: dict,
    since: str,
    hours: int,
    client: LangfuseClient,
    eval_config: dict,
    limit: int,
    dry_run: bool,
    force: bool,
    verbose: bool,
    summary: dict,
    config_path: str,
) -> None:
    name = evaluator.get("name")
    score_name = evaluator.get("scoreName")
    score_type = evaluator.get("scoreType", "NUMERIC")
    agent = evaluator.get("agent")  # 可选；填了启用 Dataset Run 写入到 {agent}-online-temp

    # rubric 来源
    rubric = evaluator.get("rubric")
    if evaluator.get("rubricFile"):
        config_dir = Path(config_path).parent
        rubric_path = config_dir / evaluator["rubricFile"]
        if not rubric_path.exists():
            click.echo(f"❌ rubricFile 文件不存在: {rubric_path}", err=True)
            summary["errors"] += 1
            return
        full_content = rubric_path.read_text("utf-8")
        section_name = evaluator.get("rubricSection", "打分规则")
        pattern = rf"^##\s+\d+\.\s*{re.escape(section_name)}"
        m = re.search(pattern, full_content, re.MULTILINE)
        if m:
            rubric = full_content[m.start():]
        else:
            rubric = full_content
            click.echo(f"   ⚠️ 未找到「{section_name}」章节，将使用整篇文档作为 rubric")

    if not name or not score_name or not rubric:
        click.echo("❌ 评估器配置不完整，需要 name / scoreName / rubric 或 rubricFile", err=True)
        summary["errors"] += 1
        return

    click.echo(f"── 评估器: {score_name} ──")
    click.echo(f"   目标 Observation: {name}")

    observations = client.get_observations(name, since, limit)
    click.echo(f"   拉取到 {len(observations)} 条 observation")
    summary["total"] += len(observations)

    if not observations:
        return

    # 仅拉取观察窗口内的 score，避免 score 量大时全表扫描
    existing_scores = client.get_scores(score_name, from_timestamp=since)
    scored_keys = {
        f"{s['traceId']}|{s.get('observationId', '')}" for s in existing_scores
    }

    # Dataset Run 写入收集（仅 evaluator 配了 agent 且非 dry-run 时启用）
    online_dataset = _online_dataset_name(agent) if agent else None
    online_evaluations: list[tuple[dict, float, str]] = []  # (obs, score, item_id)

    eval_count = 0
    for obs in observations:
        key = f"{obs['traceId']}|{obs['id']}"
        if not force and key in scored_keys:
            if verbose:
                click.echo(f"   ⏭  已有分数，跳过: {obs['id']}")
            summary["skipped"] += 1
            continue

        input_val = _extract_field(obs, evaluator.get("input", "$.input"))
        output_val = _extract_field(obs, evaluator.get("output", "$.output"))

        if not input_val and not output_val:
            if verbose:
                click.echo(f"   ⚠️  input/output 为空，跳过: {obs['id']}")
            summary["skipped"] += 1
            continue

        prompt = rubric.replace(
            "{{input}}", _truncate(_stringify(input_val), 8000)
        ).replace(
            "{{output}}", _truncate(_stringify(output_val), 8000)
        )

        try:
            result = _call_judge(prompt, eval_config)
            score = _parse_score(result, score_type, evaluator.get("scoreValues"))

            if score is None:
                click.echo(
                    f'   ❌ 无法解析评分: "{_truncate(result, 80)}" (obs: {obs["id"]})',
                    err=True,
                )
                summary["errors"] += 1
                continue

            eval_count += 1
            summary["scored"] += 1
            icon = "✅" if score >= 0.8 else ("⚠️" if score >= 0.4 else "❌")
            click.echo(
                f"   {icon} {score} | "
                f"{_truncate(_stringify(input_val), 40)} → "
                f"{_truncate(_stringify(output_val), 40)}"
            )

            if not dry_run:
                # BOOLEAN 类型 Langfuse 期望 0/1 整数；其他类型保持 float
                write_value = (
                    int(round(score)) if score_type == "BOOLEAN" else score
                )
                client.write_score({
                    "traceId": obs["traceId"],
                    "observationId": obs["id"],
                    "name": score_name,
                    "value": write_value,
                    "dataType": score_type,
                    "source": "API",
                    "comment": f"eval-online 自动评估 ({eval_config['model_name']})",
                })
                summary["written"] += 1

                # Dataset Run 写入：把这条线上 obs 转为 online-temp 的 dataset item
                if online_dataset:
                    try:
                        item = client.upsert_dataset_item({
                            "datasetName": online_dataset,
                            "input": obs.get("input"),
                            "expectedOutput": None,
                            "metadata": {
                                "source": "eval-online",
                                "observation_name": name,
                                "score_name": score_name,
                                "score_value": float(score),
                            },
                            "sourceTraceId": obs["traceId"],
                            "sourceObservationId": obs["id"],
                        })
                        item_id = item.get("id")
                        if item_id:
                            online_evaluations.append((obs, float(score), item_id))
                    except Exception as e:
                        click.echo(
                            f"   ⚠️ dataset item 写入失败（不影响 score）: {e}",
                            err=True,
                        )

        except Exception as e:
            click.echo(f'   ❌ 评估失败 ({obs["id"]}): {e}', err=True)
            summary["errors"] += 1

        time.sleep(0.2)

    click.echo(f"   完成: 评估 {eval_count} 条")

    # 写 Dataset Run（仅 evaluator 配了 agent 且收集到数据时）
    if online_dataset and online_evaluations and not dry_run:
        run_name = _online_run_name(agent, score_name, datetime.now(timezone.utc))
        pass_count = sum(1 for (_, s, _) in online_evaluations if s >= 0.5)
        total = len(online_evaluations)
        run_metadata = {
            "source": "eval-online",
            "agent": agent,
            "evaluator_name": name,
            "score_name": score_name,
            "score_type": score_type,
            "judge_model": eval_config["model_name"],
            "time_window_hours": hours,
            "since": since,
            "total_evaluated": total,
            "pass_count": pass_count,
            "fail_count": total - pass_count,
            "pass_rate": pass_count / total if total else 0.0,
        }
        first_metadata: dict | None = run_metadata
        posted = 0
        for (obs, _score, item_id) in online_evaluations:
            try:
                client.create_dataset_run_item(
                    run_name=run_name,
                    dataset_item_id=item_id,
                    trace_id=obs["traceId"],
                    observation_id=obs["id"],
                    metadata=first_metadata,
                    run_description=(
                        f"eval-online {score_name} for {agent} ({hours}h window)"
                        if first_metadata else None
                    ),
                )
                posted += 1
                first_metadata = None  # 仅第一个 item 带 metadata（first-write-wins）
            except Exception as e:
                click.echo(
                    f"   ⚠️ run-item 写入失败 (obs={obs.get('id', '?')[:16]}...): {e}",
                    err=True,
                )

        summary.setdefault("runs", []).append({
            "agent": agent,
            "run_name": run_name,
            "items": posted,
            "pass_rate": run_metadata["pass_rate"],
        })
        click.echo(f"   📦 Dataset Run: {run_name}（{posted}/{total} items）")


@click.command()
@click.option("--config", "config_path", default="eval-online.yaml", help="评估配置文件路径")
@click.option("--hours", default=24, type=int, help="拉取最近 n 小时的数据")
@click.option("--limit", default=50, type=int, help="每个 observation name 最多处理 n 条")
@click.option("--dry-run", is_flag=True, help="只拉取和评估，不写回 Langfuse")
@click.option("--force", is_flag=True, help="跳过已有分数的检查，强制重新评估")
@click.option("--verbose", is_flag=True, help="打印详细日志")
def main(config_path: str, hours: int, limit: int, dry_run: bool, force: bool, verbose: bool):
    """拉取 Langfuse 线上 Observation → LLM 评估 → 写回 Score。"""
    init_env()

    cfg_path = Path.cwd() / config_path
    if not cfg_path.exists():
        raise click.ClickException(f"配置文件不存在：{cfg_path}")

    config = load_yaml(cfg_path)
    if not config or not isinstance(config.get("evaluators"), list) or not config["evaluators"]:
        raise click.ClickException("配置文件必须包含 evaluators 数组")

    langfuse_cfg = get_langfuse_config()
    eval_cfg = get_eval_model_config()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    click.echo("╔══════════════════════════════════════════════════╗")
    click.echo("║           eval-online · 线上评估批处理            ║")
    click.echo("╚══════════════════════════════════════════════════╝")
    click.echo(f"  Langfuse  : {langfuse_cfg['base_url']}")
    click.echo(f"  评估模型  : {eval_cfg['model_name']} @ {eval_cfg['base_url']}")
    click.echo(f"  时间范围  : 最近 {hours} 小时 (since {since})")
    click.echo(f"  每名称上限: {limit} 条")
    click.echo(f"  模式      : {'🧪 DRY-RUN（不写回）' if dry_run else '🚀 正式写回'}")
    click.echo(f"  评估器数量: {len(config['evaluators'])}")
    click.echo("")

    summary = {"total": 0, "scored": 0, "skipped": 0, "errors": 0, "written": 0, "runs": []}

    with LangfuseClient(langfuse_cfg) as client:
        # 跑前清空所有 agent 的 online-temp dataset（覆盖式策略，按用户约定）
        online_agents: set[str] = {
            e["agent"] for e in config["evaluators"]
            if isinstance(e.get("agent"), str)
        }
        if online_agents and not dry_run:
            click.echo("━━━ 清空 online-temp datasets ━━━")
            for agent_name in sorted(online_agents):
                ds_name = _online_dataset_name(agent_name)
                if not client.dataset_exists(ds_name):
                    client.create_dataset(
                        ds_name,
                        f"eval-online 工作区（覆盖式），auto-managed for {agent_name}",
                    )
                    click.echo(f"  ✨ 新建 {ds_name}")
                deleted = client.delete_all_dataset_items(ds_name)
                click.echo(f"  🧹 清空 {ds_name}（删除 {deleted} items）")
            click.echo("")

        for evaluator in config["evaluators"]:
            _process_evaluator(
                evaluator, since, hours, client, eval_cfg, limit,
                dry_run, force, verbose, summary, str(cfg_path),
            )

    click.echo("")
    click.echo("═══════════════════ 汇总 ═══════════════════")
    click.echo(f"  拉取 Observation : {summary['total']}")
    click.echo(f"  已有分数（跳过） : {summary['skipped']}")
    click.echo(f"  本次评估         : {summary['scored']}")
    click.echo(f"  写回 Langfuse    : {summary['written']}")
    click.echo(f"  评估失败         : {summary['errors']}")

    runs = summary.get("runs") or []
    if runs:
        click.echo(f"  Dataset Runs     : {len(runs)}")
        for r in runs:
            click.echo(
                f"    📦 {r['run_name']}（{r['items']} items, pass_rate={r['pass_rate']:.2%}）"
            )
    click.echo("")

    if summary["errors"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
