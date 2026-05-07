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


# ── 字段提取 ──

def _extract_field(obs: dict, jsonpath: str) -> Any:
    """简易 JSONPath 提取（支持 $.key、$[n]、$.key[n].subkey 等）。"""
    if not jsonpath or jsonpath == "$.input":
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

    existing_scores = client.get_scores(score_name)
    scored_keys = {
        f"{s['traceId']}|{s.get('observationId', '')}" for s in existing_scores
    }

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
                client.write_score({
                    "traceId": obs["traceId"],
                    "observationId": obs["id"],
                    "name": score_name,
                    "value": score,
                    "dataType": score_type,
                    "source": "API",
                    "comment": f"eval-online 自动评估 ({eval_config['model_name']})",
                })
                summary["written"] += 1

        except Exception as e:
            click.echo(f'   ❌ 评估失败 ({obs["id"]}): {e}', err=True)
            summary["errors"] += 1

        time.sleep(0.2)

    click.echo(f"   完成: 评估 {eval_count} 条")


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

    summary = {"total": 0, "scored": 0, "skipped": 0, "errors": 0, "written": 0}

    with LangfuseClient(langfuse_cfg) as client:
        for evaluator in config["evaluators"]:
            _process_evaluator(
                evaluator, since, client, eval_cfg, limit,
                dry_run, force, verbose, summary, str(cfg_path),
            )

    click.echo("")
    click.echo("═══════════════════ 汇总 ═══════════════════")
    click.echo(f"  拉取 Observation : {summary['total']}")
    click.echo(f"  已有分数（跳过） : {summary['skipped']}")
    click.echo(f"  本次评估         : {summary['scored']}")
    click.echo(f"  写回 Langfuse    : {summary['written']}")
    click.echo(f"  评估失败         : {summary['errors']}")
    click.echo("")

    if summary["errors"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
