"""
eval-compare — 对比两次 PromptFoo 评估结果，生成差异报告。

适用于 A/B 测试、Prompt 迭代前后对比、回归检测。

用法：
  eval-compare --baseline <baseline.json> --candidate <candidate.json> [--output <diff.md>]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click


def _load_results(file_path: str) -> list[dict]:
    p = Path(file_path)
    if not p.exists():
        click.echo(f"❌ 文件不存在：{file_path}", err=True)
        raise SystemExit(1)
    raw = json.loads(p.read_text("utf-8"))
    results = raw.get("results", raw)
    return results if isinstance(results, list) else results.get("results", [])


def _calc_stats(results: list[dict]) -> dict:
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


def _get_test_key(result: dict) -> str:
    return json.dumps(result.get("vars", {}), sort_keys=True)


def _generate_compare_report(
    base_stats: dict,
    cand_stats: dict,
    regressions: list[dict],
    improvements: list[dict],
    baseline_path: str,
    candidate_path: str,
) -> str:
    rate_diff = float(cand_stats["rate"]) - float(base_stats["rate"])
    rate_diff_str = f"{rate_diff:+.1f}"
    rate_emoji = "📈" if rate_diff > 0 else ("📉" if rate_diff < 0 else "➡️")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _diff_str(a: int, b: int) -> str:
        d = b - a
        return f"+{d}" if d > 0 else str(d)

    lines = [
        "# 📊 评估对比报告",
        "",
        f"> 生成时间：{now}",
        f"> Baseline：`{baseline_path}`",
        f"> Candidate：`{candidate_path}`",
        "",
        "## 总体对比",
        "",
        "| 指标 | Baseline | Candidate | 变化 |",
        "|------|----------|-----------|------|",
        f"| 总测试数 | {base_stats['total']} | {cand_stats['total']} | {_diff_str(base_stats['total'], cand_stats['total'])} |",
        f"| ✅ 通过 | {base_stats['pass']} | {cand_stats['pass']} | {_diff_str(base_stats['pass'], cand_stats['pass'])} |",
        f"| ❌ 失败 | {base_stats['fail']} | {cand_stats['fail']} | {_diff_str(base_stats['fail'], cand_stats['fail'])} |",
        f"| **通过率** | **{base_stats['rate']}%** | **{cand_stats['rate']}%** | **{rate_emoji} {rate_diff_str}%** |",
        "",
    ]

    if regressions:
        lines.extend([
            f"## 🔴 回归 ({len(regressions)} 个用例)",
            "",
            "> 以下用例在 Baseline 中通过，但在 Candidate 中失败，需要重点关注。",
            "",
        ])
        show = min(len(regressions), 5)
        for i in range(show):
            r = regressions[i]
            lines.extend([
                f"### {i + 1}. 回归用例",
                "",
                "**输入：**",
                "```json",
                json.dumps(r["vars"], ensure_ascii=False, indent=2)[:200],
                "```",
                f"**Baseline 输出：** {r['base_output']}...",
                "",
                f"**Candidate 输出：** {r['cand_output']}...",
                "",
                "---",
                "",
            ])
        if len(regressions) > show:
            lines.extend([f"> ℹ️ 还有 {len(regressions) - show} 个回归用例未列出。", ""])

    if improvements:
        lines.extend([
            f"## 🟢 改善 ({len(improvements)} 个用例)",
            "",
            "> 以下用例在 Baseline 中失败，但在 Candidate 中通过。",
            "",
        ])
        show = min(len(improvements), 5)
        for i in range(show):
            lines.append(f"- {json.dumps(improvements[i]['vars'], ensure_ascii=False)[:150]}")
        if len(improvements) > show:
            lines.append(f"- ... 还有 {len(improvements) - show} 个")
        lines.append("")

    lines.extend(["## 结论", ""])
    if not regressions and rate_diff >= 0:
        lines.append("✅ **安全升级**：无回归，通过率未下降。")
    elif regressions:
        lines.append(
            f"⚠️ **存在回归**：{len(regressions)} 个用例从 PASS 变为 FAIL，建议排查后再发布。"
        )
    else:
        lines.append(
            f"📉 **通过率下降 {rate_diff_str}%**，但无可匹配的回归用例，可能是新增测试导致。"
        )
    lines.append("")
    return "\n".join(lines)


@click.command()
@click.option("--baseline", required=True, help="Baseline 评估结果 JSON")
@click.option("--candidate", required=True, help="Candidate 评估结果 JSON")
@click.option("--output", "output_path", default="output/compare-report.md", help="对比报告输出路径")
def main(baseline: str, candidate: str, output_path: str):
    """对比两次 PromptFoo 评估结果，生成差异报告。"""
    base_results = _load_results(baseline)
    cand_results = _load_results(candidate)
    base_stats = _calc_stats(base_results)
    cand_stats = _calc_stats(cand_results)

    base_map = {_get_test_key(r): r for r in base_results}

    regressions = []
    improvements = []

    for r in cand_results:
        key = _get_test_key(r)
        base = base_map.get(key)
        if base is None:
            continue
        cand_pass = r.get("success", r.get("pass")) is True
        base_pass = base.get("success", base.get("pass")) is True

        if base_pass and not cand_pass:
            regressions.append({
                "vars": r.get("vars", {}),
                "base_output": (base.get("response", {}).get("output") or base.get("output", ""))[:150],
                "cand_output": (r.get("response", {}).get("output") or r.get("output", ""))[:150],
            })
        elif not base_pass and cand_pass:
            improvements.append({"vars": r.get("vars", {})})

    report = _generate_compare_report(
        base_stats, cand_stats, regressions, improvements, baseline, candidate
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    click.echo(f"📊 对比报告已生成 → {output_path}")
    click.echo(
        f"   Baseline {base_stats['rate']}% → Candidate {cand_stats['rate']}%  "
        f"({'+' if float(cand_stats['rate']) >= float(base_stats['rate']) else ''}"
        f"{float(cand_stats['rate']) - float(base_stats['rate']):.1f}%)"
    )
    if regressions:
        click.echo(f"   🔴 {len(regressions)} 个回归，{len(improvements)} 个改善")
    else:
        click.echo(f"   ✅ 无回归，{len(improvements)} 个改善")


if __name__ == "__main__":
    main()
