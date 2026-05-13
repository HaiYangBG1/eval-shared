"""
eval-dspy-pipeline — DSPy 优化 + PromptFoo A/B 对比 完整流水线。

串联 eval-dspy-optimize 和 eval-promptfoo-ab，一条命令完成：
  1. DSPy 优化 → 上传 Langfuse staging
  2. 检查优化结果（delta ≤ 0 则短路退出）
  3. PromptFoo A/B 对比（production vs staging）
  4. 生成统一决策报告

用法：
  eval-dspy-pipeline --config <path> [--skip-optimize] [--dry-run]
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.ab_verdict import (
    AB_VERDICT_LABELS,
    ABVerdict,
    verdict_from_ab_summary,
)
from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.yaml_utils import load_yaml


def _run_cmd(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    """运行外部命令并实时输出到终端。"""
    click.echo(f"\n  🔄 {label}")
    click.echo(f"     $ {' '.join(cmd)}")
    result = subprocess.run(cmd)  # inherit stdio for live output
    if result.returncode != 0:
        raise click.ClickException(f"{label} 失败 (exit code {result.returncode})")
    return result


def _find_latest_report(dataset: str) -> Path | None:
    """查找最新的 DSPy .report.json。"""
    output_dir = Path("output")
    if not output_dir.exists():
        return None
    reports = sorted(
        output_dir.glob(f"optimized_{dataset}_*.report.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _extract_agent_from_config(config: dict) -> str:
    """从 DSPy 配置中推断 agent 名称。"""
    # 优先用 dataset 名（大多数情况与 agent 名一致）
    dataset = config.get("dataset", "")
    if dataset:
        return dataset
    # fallback: 从 prompt_name 推断
    prompt_name = config.get("output", {}).get("prompt_name", "")
    if prompt_name.endswith("-prompt"):
        return prompt_name[:-7]
    return ""


def _annotate_prompt(prompt_name: str, verdict: ABVerdict) -> None:
    """在 Langfuse staging Prompt 上写入 A/B verdict label（三态枚举之一）。

    剥离全部历史 A/B 枚举值（保证 label 集合永远在 3 个枚举内不膨胀），
    再追加新的 verdict。详细数字明细不再编码进 label，由 Dataset Run metadata 承载。
    """
    try:
        with LangfuseClient() as client:
            staging = client.get_prompt(prompt_name, label="staging")
            version = staging.get("version")
            if not version:
                click.echo(f"  ⚠️ 无法获取 {prompt_name} staging 版本号，跳过标注")
                return

            # 剥离已有 A/B 枚举值（保证集合不膨胀），'latest' 由 Langfuse 自管不动
            existing_labels = staging.get("labels", [])
            new_labels = [
                lb for lb in existing_labels
                if lb not in AB_VERDICT_LABELS and lb != "latest"
            ]
            new_labels.append(verdict.value)

            client.update_prompt_labels(prompt_name, version, new_labels)
            click.echo(f"  ✅ 已标注 Langfuse: {prompt_name} v{version} ← {verdict.value}")

    except Exception as e:
        click.echo(f"  ⚠️ Langfuse 标注失败（不影响流程）: {e}")


def _generate_pipeline_report(
    agent: str,
    dspy_report: dict | None,
    ab_report_path: str | None,
    skipped_ab: bool,
    skip_reason: str = "",
) -> str:
    """生成统一决策报告（Markdown）。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# 🚀 DSPy 优化决策报告 — {agent}",
        "",
        f"> 生成时间：{now}",
        "",
    ]

    # ── Part 1: DSPy 优化摘要 ──
    lines.extend(["## Part 1: DSPy 优化结果", ""])
    if dspy_report:
        baseline = dspy_report.get("baseline_score", 0)
        optimized = dspy_report.get("optimized_score", 0)
        delta = dspy_report.get("delta", 0)
        delta_emoji = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        lines.extend([
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 数据集 | {dspy_report.get('dataset', '?')} |",
            f"| 总样本数 | {dspy_report.get('total_examples', '?')} |",
            f"| 训练/验证 | {dspy_report.get('train_size', '?')}/{dspy_report.get('dev_size', '?')} |",
            f"| 优化器 | {dspy_report.get('optimizer', '?')} |",
            f"| 评估指标 | {dspy_report.get('metric', '?')} |",
            f"| **基线准确率** | **{baseline:.2%}** |",
            f"| **优化后准确率** | **{optimized:.2%}** |",
            f"| **变化** | **{delta_emoji} {delta:+.2%}** |",
            "",
        ])
    else:
        lines.extend(["⚠️ 未找到 DSPy 优化报告。", ""])

    # ── Part 2: PromptFoo A/B 对比 ──
    lines.extend(["## Part 2: PromptFoo A/B 对比", ""])
    if skipped_ab:
        lines.extend([
            "⏭️ 已跳过 PromptFoo A/B 对比。",
            "",
            f"**原因**：{skip_reason}",
            "",
        ])
    elif ab_report_path and Path(ab_report_path).exists():
        # 嵌入 A/B 报告内容（跳过一级标题和元信息）
        ab_content = Path(ab_report_path).read_text("utf-8")
        ab_lines = ab_content.split("\n")
        for i, line in enumerate(ab_lines):
            if line.startswith("## "):
                ab_content = "\n".join(ab_lines[i:])
                break
        lines.extend([ab_content, ""])
    else:
        lines.extend(["⚠️ 未找到 PromptFoo A/B 对比报告。", ""])

    # ── Part 3: 最终决策建议 ──
    lines.extend(["## Part 3: 决策建议", ""])

    if skipped_ab:
        lines.extend([
            "### ❌ 不建议升级",
            "",
            f"{skip_reason}",
            "",
        ])
    elif dspy_report and ab_report_path and Path(ab_report_path).exists():
        ab_text = Path(ab_report_path).read_text("utf-8")
        has_regression = "🔴 回归" in ab_text
        safe_upgrade = "✅ **安全升级**" in ab_text

        if safe_upgrade:
            delta = dspy_report.get("delta", 0)
            lines.extend([
                "### ✅ 建议：执行升级",
                "",
                f"DSPy 优化产生了 {delta:+.2%} 的提升，"
                "且 PromptFoo A/B 对比满足净改善策略。",
                "",
                "执行以下命令完成升级：",
                "```bash",
                f"npm run promote -- --agent {agent}",
                "```",
                "",
            ])
        elif has_regression:
            lines.extend([
                "### ⚠️ 建议：暂缓升级",
                "",
                "DSPy 优化显示正向提升，但 PromptFoo 回归测试发现回归用例。",
                "建议排查回归原因后再决定是否升级。",
                "",
            ])
        else:
            lines.extend([
                "### 🤔 建议：人工审核",
                "",
                "请查看上方 A/B 对比详情，人工判断是否接受变更。",
                "",
            ])
    else:
        lines.extend([
            "### 🤔 建议：信息不足",
            "",
            "缺少完整的评估数据，请检查流程是否正确执行。",
            "",
        ])

    return "\n".join(lines)


# ── CLI 入口 ──


@click.command()
@click.option("--config", "config_path", required=True, help="DSPy 优化配置文件路径（YAML）")
@click.option("--skip-optimize", is_flag=True, help="跳过 DSPy 优化，直接进行 A/B 对比（适用于已优化过的情况）")
@click.option("--dry-run", is_flag=True, help="传递给子命令的 dry-run 标志")
@click.option("--seed", type=int, default=42, help="随机种子（传递给 DSPy 优化器）")
def main(config_path: str, skip_optimize: bool, dry_run: bool, seed: int):
    """DSPy 优化 + PromptFoo A/B 对比 完整流水线。"""
    init_env()

    # 加载配置
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise click.ClickException(f"配置文件不存在：{config_path}")

    config = load_yaml(cfg_file)
    if not config:
        raise click.ClickException("配置文件为空")

    agent = _extract_agent_from_config(config)
    if not agent:
        raise click.ClickException(
            "无法从配置中推断 agent 名称，请确保配置含 dataset 或 output.prompt_name"
        )

    dataset = config.get("dataset", agent)

    # ═══ Banner ═══
    click.echo("")
    click.echo("╔══════════════════════════════════════════════════════╗")
    click.echo("║   eval-dspy-pipeline · DSPy 优化 + PromptFoo 验证    ║")
    click.echo("╚══════════════════════════════════════════════════════╝")
    click.echo(f"  Agent     : {agent}")
    click.echo(f"  配置文件  : {config_path}")
    click.echo(f"  跳过优化  : {'是' if skip_optimize else '否'}")
    click.echo(f"  模式      : {'🧪 DRY-RUN' if dry_run else '🚀 正式运行'}")
    click.echo("")

    # ═══ Phase 1: DSPy 优化 ═══
    click.echo("=" * 56)
    click.echo("  Phase 1: DSPy Prompt 优化")
    click.echo("=" * 56)

    if skip_optimize:
        click.echo("  ⏭️  已跳过 DSPy 优化（--skip-optimize）")
    else:
        optimize_cmd = ["eval-dspy-optimize", "--config", config_path, "--seed", str(seed)]
        if dry_run:
            optimize_cmd.append("--dry-run")
        _run_cmd(optimize_cmd, "DSPy 优化")

    if dry_run:
        click.echo("\n🧪 DRY-RUN 完成 — DSPy 优化验证通过。")
        click.echo("   完整流程需去掉 --dry-run 执行。")
        return

    # ═══ 读取 DSPy 报告，检查是否值得继续 ═══
    dspy_report_path = _find_latest_report(dataset)
    dspy_report: dict | None = None

    if dspy_report_path:
        dspy_report = json.loads(dspy_report_path.read_text("utf-8"))
        delta = dspy_report.get("delta", 0)
        baseline = dspy_report.get("baseline_score", 0)
        optimized = dspy_report.get("optimized_score", 0)

        click.echo(f"\n  📋 DSPy 报告: {dspy_report_path}")
        click.echo(f"     基线 {baseline:.2%} → 优化后 {optimized:.2%} (Δ {delta:+.2%})")

        # 短路判断：优化无提升则跳过 PromptFoo 对比
        if delta <= 0 and not skip_optimize:
            click.echo("\n  ⚠️  DSPy 优化未产生正向提升，跳过 PromptFoo A/B 对比。")
            report = _generate_pipeline_report(
                agent, dspy_report, None,
                skipped_ab=True,
                skip_reason=f"DSPy 优化 delta = {delta:+.2%}，未产生正向提升，无需进行 PromptFoo 验证。",
            )
            report_path = f"output/{agent}-pipeline-report.md"
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(report, encoding="utf-8")
            click.echo(f"\n📊 决策报告 → {report_path}")

            # 跳过 A/B 也要在 staging prompt 上打 🟰，避免后续 promote 看到旧的评估状态
            prompt_name = config.get("output", {}).get("prompt_name", f"{agent}-prompt")
            _annotate_prompt(prompt_name, ABVerdict.SAME)

            click.echo("\n❌ 本次优化未产生提升，不建议切换。")
            return
    else:
        click.echo("\n  ⚠️  未找到 DSPy .report.json，继续执行 A/B 对比...")

    # ═══ Phase 2: PromptFoo A/B 对比 ═══
    click.echo("")
    click.echo("=" * 56)
    click.echo("  Phase 2: PromptFoo A/B 对比")
    click.echo("=" * 56)

    ab_report_path = f"output/{agent}-ab-report.md"
    ab_cmd = [
        "eval-promptfoo-ab",
        "--agent", agent,
        "--baseline-label", "production",
        "--candidate-label", "staging",
    ]
    # ab.tolerance / ab.dataset / ab.sync_dataset 配置透传（dspy-optimize.yaml 可覆盖 CLI 默认）
    ab_cfg = config.get("ab", {}) if isinstance(config.get("ab"), dict) else {}
    if "tolerance" in ab_cfg:
        ab_cmd.extend(["--tolerance", str(float(ab_cfg["tolerance"]))])
    if "dataset" in ab_cfg and isinstance(ab_cfg["dataset"], str):
        ab_cmd.extend(["--dataset", ab_cfg["dataset"]])
    if ab_cfg.get("sync_dataset") is True:
        ab_cmd.append("--sync-dataset")
    _run_cmd(ab_cmd, "PromptFoo A/B 对比")

    # ═══ Phase 3: 统一决策报告 ═══
    click.echo("")
    click.echo("=" * 56)
    click.echo("  Phase 3: 生成统一决策报告")
    click.echo("=" * 56)

    report = _generate_pipeline_report(
        agent, dspy_report, ab_report_path,
        skipped_ab=False,
    )

    pipeline_report_path = f"output/{agent}-pipeline-report.md"
    Path(pipeline_report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(pipeline_report_path).write_text(report, encoding="utf-8")

    click.echo(f"\n📊 统一决策报告 → {pipeline_report_path}")

    # ═══ Phase 4: 标注 Langfuse Prompt ═══
    click.echo("")
    click.echo("=" * 56)
    click.echo("  Phase 4: 标注 Langfuse Prompt")
    click.echo("=" * 56)

    prompt_name = config.get("output", {}).get("prompt_name", f"{agent}-prompt")
    ab_summary_path = f"output/{agent}-ab-summary.json"

    if Path(ab_summary_path).exists():
        ab_summary = json.loads(Path(ab_summary_path).read_text("utf-8"))
        verdict = verdict_from_ab_summary(ab_summary)
        _annotate_prompt(prompt_name, verdict)
    else:
        click.echo("  ⚠️ 未找到 A/B 摘要文件，跳过标注")

    # 终端最终建议
    if Path(ab_report_path).exists():
        ab_text = Path(ab_report_path).read_text("utf-8")
        if "✅ **安全升级**" in ab_text:
            click.echo(f"\n🎉 建议升级！执行: npm run promote -- --agent {agent}")
        elif "🔴 回归" in ab_text:
            click.echo("\n⚠️  存在回归用例，建议排查后再决定。详见报告。")
        else:
            click.echo("\n🤔 请查看报告做最终决策。")
    click.echo("")


if __name__ == "__main__":
    main()
