"""
eval-report — 读取 PromptFoo 评估输出，生成 Markdown 格式的摘要报告。

用法：
  eval-report [--input <output.json>] [--output <report.md>] [--agent <name>]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click


def _calc_stats(results: list[dict]) -> dict:
    total = pass_count = fail_count = error_count = 0
    failed_cases = []
    for r in results:
        total += 1
        success = r.get("success", r.get("pass"))
        if success is True:
            pass_count += 1
        elif success is False:
            fail_count += 1
            components = (
                r.get("gradingResult", {}).get("componentResults")
                or r.get("assertionResults")
                or []
            )
            fail_reasons = [
                (c.get("reason") or c.get("assertion", {}).get("value") or "Unknown")[:200]
                for c in components
                if not c.get("pass")
            ][:3]
            failed_cases.append({
                "vars": r.get("vars", {}),
                "output": (
                    r.get("response", {}).get("output")
                    or r.get("output", "")
                )[:200],
                "fail_reasons": fail_reasons,
            })
        else:
            error_count += 1

    rate = f"{(pass_count / total * 100):.1f}" if total > 0 else "0.0"
    return {
        "total": total,
        "pass": pass_count,
        "fail": fail_count,
        "error": error_count,
        "rate": rate,
        "failed_cases": failed_cases,
    }


def _generate_report(stats: dict, input_path: str, agent: str | None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 📊 评估报告",
        "",
        f"> 生成时间：{now}",
    ]
    if agent:
        lines.append(f"> Agent：{agent}")
    lines.extend([
        f"> 数据来源：`{input_path}`",
        "",
        "## 摘要",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 总测试数 | {stats['total']} |",
        f"| ✅ 通过 | {stats['pass']} |",
        f"| ❌ 失败 | {stats['fail']} |",
        f"| ⚠️ 错误 | {stats['error']} |",
        f"| **通过率** | **{stats['rate']}%** |",
        "",
    ])

    rate = float(stats["rate"])
    if rate >= 95:
        lines.append("> 🟢 优秀：通过率 ≥ 95%")
    elif rate >= 80:
        lines.append("> 🟡 良好：通过率 80% - 95%，部分用例需要关注")
    else:
        lines.append("> 🔴 需改进：通过率 < 80%，建议排查失败用例")
    lines.append("")

    failed = stats["failed_cases"]
    if failed:
        lines.append("## 失败用例")
        lines.append("")
        show = min(len(failed), 10)
        for i in range(show):
            c = failed[i]
            input_str = json.dumps(c["vars"], ensure_ascii=False, indent=2)[:150]
            lines.extend([
                f"### {i + 1}. 失败用例",
                "",
                "**输入：**",
                "```json",
                input_str,
                "```",
                "",
                f"**输出（截取）：** {c['output']}...",
                "",
            ])
            if c["fail_reasons"]:
                lines.append("**失败原因：**")
                for reason in c["fail_reasons"]:
                    lines.append(f"- {reason}")
            lines.extend(["", "---", ""])

        if len(failed) > show:
            lines.append(f"> ℹ️ 还有 {len(failed) - show} 个失败用例未列出，请查看原始输出文件。")
            lines.append("")

    return "\n".join(lines)


@click.command()
@click.option("--input", "input_path", default=None, help="PromptFoo 输出 JSON 文件路径")
@click.option("--output", "output_path", default=None, help="报告输出路径")
@click.option("--agent", default=None, help="Agent 名称")
def main(input_path: str | None, output_path: str | None, agent: str | None):
    """读取 PromptFoo 评估输出，生成 Markdown 摘要报告。"""
    if input_path is None:
        input_path = (
            f"agents/{agent}/output/latest.json" if agent else "output/latest.json"
        )
    if output_path is None:
        output_path = input_path.replace(".json", "-report.md")

    p = Path(input_path)
    if not p.exists():
        click.echo(f"❌ 找不到评估输出文件：{input_path}", err=True)
        click.echo("💡 提示：先运行 promptfoo eval -o output/latest.json 生成输出文件", err=True)
        raise SystemExit(1)

    raw = json.loads(p.read_text("utf-8"))
    results_obj = raw.get("results", raw)
    eval_results = (
        results_obj if isinstance(results_obj, list) else results_obj.get("results", [])
    )

    stats = _calc_stats(eval_results)
    report = _generate_report(stats, input_path, agent)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    click.echo(f"📊 报告已生成 → {output_path}")
    click.echo(f"   通过率：{stats['rate']}%  ({stats['pass']}/{stats['total']})")
    if stats["fail"] > 0:
        click.echo(f"   ❌ {stats['fail']} 个用例失败，详情见报告。")


if __name__ == "__main__":
    main()
