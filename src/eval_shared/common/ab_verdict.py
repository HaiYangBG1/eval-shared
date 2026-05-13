"""A/B 评估结果的三态枚举。

dspy_pipeline 用它打 Langfuse Prompt label，promote_prompt 用它做门禁。
"""

from __future__ import annotations

from enum import Enum


class ABVerdict(str, Enum):
    """A/B 对比结论的三态枚举。

    - ✅ BETTER：候选明确优于基线（净改善：改善数 > 回归数 且 通过率不降）
    - ❌ WORSE ：候选存在回归 或 通过率下降
    - 🟰 SAME  ：变化不显著（含 DSPy 优化跳过 A/B 的情况）
    """

    BETTER = "A/B ✅"
    WORSE = "A/B ❌"
    SAME = "A/B 🟰"


# 全部 A/B verdict label 的集合，用于 promote_prompt 在 promote 时剥离评估状态
AB_VERDICT_LABELS: frozenset[str] = frozenset(v.value for v in ABVerdict)


def compute_verdict(
    *,
    rate_diff: float,
    regressions: int,
    improvements: int,
    tolerance: float = 0.0,
) -> ABVerdict:
    """单 dataset 三态判定。

    判定优先级：
    1. regressions>0 或 rate_diff < -tolerance → WORSE
    2. rate_diff > tolerance 且 improvements > regressions → BETTER
    3. 其他（|rate_diff| ≤ tolerance，或净改善不足）→ SAME

    Args:
        rate_diff: 通过率变化（百分比，候选 - 基线）
        regressions: 回归用例数（baseline pass → candidate fail）
        improvements: 改善用例数（baseline fail → candidate pass）
        tolerance: 容忍阈值（百分比）。在 ±tolerance 内的变化视为不显著。
    """
    if regressions > 0 or rate_diff < -tolerance:
        return ABVerdict.WORSE
    if rate_diff > tolerance and improvements > regressions:
        return ABVerdict.BETTER
    return ABVerdict.SAME


# 多 dataset 聚合时的"最差优先"排序：WORSE < SAME < BETTER
_VERDICT_RANK: dict[ABVerdict, int] = {
    ABVerdict.WORSE: 0,
    ABVerdict.SAME: 1,
    ABVerdict.BETTER: 2,
}


def aggregate_verdicts(verdicts: list[ABVerdict]) -> ABVerdict:
    """多 dataset 聚合：取最差。

    任一 dataset 为 WORSE → 整体 WORSE；全部 BETTER → 整体 BETTER；其他 → SAME。
    阶段 6 后 promptfoo_ab 跑多 dataset（如 golden + regression）时使用。
    """
    if not verdicts:
        return ABVerdict.SAME
    return min(verdicts, key=lambda v: _VERDICT_RANK[v])


def verdict_from_ab_summary(summary: dict) -> ABVerdict:
    """从 promptfoo-ab 的 summary.json 推断三态 verdict。

    优先读顶层 `verdict` 字段（promptfoo_ab 4c 后会直接写入聚合后的 verdict）；
    若不存在则从老字段推断（兼容历史 summary，无 tolerance）。
    """
    top_verdict = summary.get("verdict")
    if isinstance(top_verdict, str) and top_verdict in AB_VERDICT_LABELS:
        return ABVerdict(top_verdict)

    # 兼容路径：老 summary 没有 verdict 字段
    safe = summary.get("safe_to_upgrade", False)
    rate_diff = float(summary.get("rate_diff", 0))
    regressions = int(summary.get("regressions", 0))

    if safe:
        return ABVerdict.BETTER
    if regressions > 0 or rate_diff < 0:
        return ABVerdict.WORSE
    return ABVerdict.SAME
