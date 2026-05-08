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
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.config import init_env


# ── 工具函数 ──


def _run_cmd(cmd: list[str], label: str, capture: bool = True) -> subprocess.CompletedProcess:
    """运行外部命令，失败时抛出异常。"""
    click.echo(f"  🔄 {label}")
    click.echo(f"     $ {' '.join(cmd)}")
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
    else:
        # 实时输出模式（用于长时间运行的命令如 PromptFoo）
        result = subprocess.run(cmd)
    if result.returncode != 0:
        if capture:
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""
            error_msg = (stderr or stdout)[-500:]
            click.echo(f"  ❌ {label} 失败:")
            if error_msg:
                click.echo(f"     {error_msg}")
        else:
            click.echo(f"  ⚠️ {label} 退出码: {result.returncode}")
        raise click.ClickException(f"{label} 返回非零退出码: {result.returncode}")
    click.echo(f"  ✅ {label} 完成")
    return result


def _sync_prompt(agent: str, label: str) -> None:
    """拉取指定 label 的 Prompt 到本地。"""
    _run_cmd(
        ["eval-sync-prompt", "--direction", "pull", "--agent", agent, "--label", label],
        f"拉取 {label} Prompt",
    )


def _run_promptfoo(agent: str, output_path: str) -> None:
    """运行 PromptFoo 评估。

    使用实时输出模式，且容忍 telemetry 超时导致的非零退出码
    （PromptFoo 的 telemetry.shutdown() 在子进程中可能超时返回 exit code 100，
    但评估结果已正确写入文件）。
    """
    config_path = f"agents/{agent}/promptfooconfig.yaml"
    cmd = ["npx", "promptfoo", "eval", "-c", config_path, "-o", output_path, "--no-cache"]
    click.echo(f"  🔄 PromptFoo 评估 → {output_path}")
    click.echo(f"     $ {' '.join(cmd)}")
    result = subprocess.run(cmd)

    # 判断成功：优先检查结果文件是否已生成
    output_exists = Path(output_path).exists()
    if result.returncode != 0 and not output_exists:
        raise click.ClickException(
            f"PromptFoo 评估失败 (exit code {result.returncode})，且未生成结果文件"
        )
    if result.returncode != 0 and output_exists:
        click.echo(f"  ⚠️ PromptFoo 退出码 {result.returncode}（结果文件已生成，忽略 telemetry 超时）")
    click.echo(f"  ✅ PromptFoo 评估完成")


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
) -> str:
    """生成 A/B 对比报告（Markdown）。"""
    rate_diff = float(cand_stats["rate"]) - float(base_stats["rate"])
    rate_emoji = "📈" if rate_diff > 0 else ("📉" if rate_diff < 0 else "➡️")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

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
    if not regressions and rate_diff >= 0:
        lines.append(f"✅ **安全升级**：无回归，通过率未下降。建议执行 `npm run promote -- --agent {agent}`。")
    elif regressions:
        lines.append(
            f"⚠️ **存在回归**：{len(regressions)} 个用例从 PASS → FAIL，建议排查后再决定。"
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
@click.option("--dry-run", is_flag=True, help="只检查环境，不执行评估")
def main(agent: str, baseline_label: str, candidate_label: str, output_dir: str, dry_run: bool):
    """PromptFoo A/B 对比测试 — 比较两个 Prompt 版本的评估结果。"""
    init_env()

    click.echo("")
    click.echo("╔══════════════════════════════════════════════════╗")
    click.echo("║       eval-promptfoo-ab · A/B 对比测试            ║")
    click.echo("╚══════════════════════════════════════════════════╝")
    click.echo(f"  Agent     : {agent}")
    click.echo(f"  Baseline  : {baseline_label}")
    click.echo(f"  Candidate : {candidate_label}")
    click.echo(f"  模式      : {'🧪 DRY-RUN' if dry_run else '🚀 正式运行'}")
    click.echo("")

    # 检查前置条件
    prompt_path = Path(f"agents/{agent}/prompt.yaml")
    config_path = Path(f"agents/{agent}/promptfooconfig.yaml")

    if not config_path.exists():
        raise click.ClickException(f"找不到 PromptFoo 配置: {config_path}")
    if not prompt_path.exists():
        raise click.ClickException(f"找不到 Prompt 文件: {prompt_path}")

    click.echo("  ✅ 前置条件检查通过")

    if dry_run:
        click.echo("\n🧪 DRY-RUN 完成 — 环境检查通过。")
        return

    # 备份当前 Prompt
    backup_path = prompt_path.with_suffix(".yaml.ab-backup")
    shutil.copy2(prompt_path, backup_path)
    click.echo(f"  📦 已备份 → {backup_path}")

    baseline_output = f"{output_dir}/{agent}-ab-baseline.json"
    candidate_output = f"{output_dir}/{agent}-ab-candidate.json"

    try:
        click.echo(f"\n━━━ Step 1/4: Baseline ({baseline_label}) 评估 ━━━")
        _sync_prompt(agent, baseline_label)
        _run_promptfoo(agent, baseline_output)

        click.echo(f"\n━━━ Step 2/4: Candidate ({candidate_label}) 评估 ━━━")
        _sync_prompt(agent, candidate_label)
        _run_promptfoo(agent, candidate_output)
    finally:
        click.echo("\n━━━ Step 3/4: 恢复 Prompt ━━━")
        if backup_path.exists():
            shutil.copy2(backup_path, prompt_path)
            backup_path.unlink()
            click.echo("  ✅ 已从备份恢复 prompt.yaml")
        else:
            click.echo("  ⚠️ 备份文件不存在，跳过恢复")

    # 生成对比报告
    click.echo("\n━━━ Step 4/4: 生成对比报告 ━━━")

    base_results = load_results(baseline_output)
    cand_results = load_results(candidate_output)
    base_stats = calc_stats(base_results)
    cand_stats = calc_stats(cand_results)
    regressions, improvements = find_regressions_and_improvements(base_results, cand_results)

    report = generate_ab_report(
        agent, baseline_label, candidate_label,
        base_stats, cand_stats, regressions, improvements,
        baseline_output, candidate_output,
    )

    report_path = f"{output_dir}/{agent}-ab-report.md"
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(report, encoding="utf-8")

    # 同时输出机器可读的 JSON 摘要（供 pipeline 编排层读取）
    summary = {
        "agent": agent,
        "baseline_label": baseline_label,
        "candidate_label": candidate_label,
        "baseline": base_stats,
        "candidate": cand_stats,
        "regressions": len(regressions),
        "improvements": len(improvements),
        "rate_diff": float(cand_stats["rate"]) - float(base_stats["rate"]),
        "safe_to_upgrade": not regressions and float(cand_stats["rate"]) >= float(base_stats["rate"]),
    }
    summary_path = f"{output_dir}/{agent}-ab-summary.json"
    Path(summary_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 终端摘要
    rate_diff = float(cand_stats["rate"]) - float(base_stats["rate"])
    rate_emoji = "📈" if rate_diff > 0 else ("📉" if rate_diff < 0 else "➡️")

    click.echo("")
    click.echo("╔══════════════════════════════════════╗")
    click.echo("║         A/B 对比结果                  ║")
    click.echo("╠══════════════════════════════════════╣")
    click.echo(f"║  {baseline_label:>10} : {base_stats['rate']:>5}% ({base_stats['pass']}/{base_stats['total']})    ║")
    click.echo(f"║  {candidate_label:>10} : {cand_stats['rate']:>5}% ({cand_stats['pass']}/{cand_stats['total']})    ║")
    click.echo(f"║  变化       : {rate_emoji} {rate_diff:+.1f}%               ║")
    if regressions:
        click.echo(f"║  🔴 回归    : {len(regressions)} 个                    ║")
    if improvements:
        click.echo(f"║  🟢 改善    : {len(improvements)} 个                    ║")
    click.echo("╚══════════════════════════════════════╝")
    click.echo(f"\n📊 对比报告 → {report_path}")

    if not regressions and rate_diff >= 0:
        click.echo(f"\n✅ 安全升级：建议执行 npm run promote -- --agent {agent}")
    elif regressions:
        click.echo(f"\n⚠️ 存在 {len(regressions)} 个回归，建议排查后再决定。")


if __name__ == "__main__":
    main()
