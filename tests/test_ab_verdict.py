from __future__ import annotations

from eval_shared.common.ab_verdict import (
    AB_VERDICT_LABELS,
    ABVerdict,
    aggregate_verdicts,
    compute_verdict,
    verdict_from_ab_summary,
)


def test_enum_values_are_user_facing_emoji_labels() -> None:
    assert ABVerdict.BETTER.value == "A/B ✅"
    assert ABVerdict.WORSE.value == "A/B ❌"
    assert ABVerdict.SAME.value == "A/B 🟰"


def test_label_set_covers_all_enum_values() -> None:
    assert AB_VERDICT_LABELS == {"A/B ✅", "A/B ❌", "A/B 🟰"}


def test_safe_to_upgrade_maps_to_better() -> None:
    summary = {"safe_to_upgrade": True, "rate_diff": 12.9, "regressions": 0}
    assert verdict_from_ab_summary(summary) == ABVerdict.BETTER


def test_regressions_present_maps_to_worse() -> None:
    summary = {"safe_to_upgrade": False, "rate_diff": 0.5, "regressions": 3}
    assert verdict_from_ab_summary(summary) == ABVerdict.WORSE


def test_negative_rate_diff_maps_to_worse_even_without_regressions() -> None:
    summary = {"safe_to_upgrade": False, "rate_diff": -2.1, "regressions": 0}
    assert verdict_from_ab_summary(summary) == ABVerdict.WORSE


def test_no_regression_and_non_negative_diff_maps_to_same() -> None:
    summary = {"safe_to_upgrade": False, "rate_diff": 0.5, "regressions": 0}
    assert verdict_from_ab_summary(summary) == ABVerdict.SAME


def test_zero_diff_maps_to_same() -> None:
    summary = {"safe_to_upgrade": False, "rate_diff": 0, "regressions": 0}
    assert verdict_from_ab_summary(summary) == ABVerdict.SAME


def test_missing_fields_default_to_same() -> None:
    """summary 不完整时不应崩溃，回退到 SAME。"""
    assert verdict_from_ab_summary({}) == ABVerdict.SAME


def test_safe_takes_priority_even_if_other_fields_look_bad() -> None:
    """safe_to_upgrade=True 时其他字段不应推翻 BETTER 判定。"""
    summary = {"safe_to_upgrade": True, "rate_diff": -0.1, "regressions": 0}
    assert verdict_from_ab_summary(summary) == ABVerdict.BETTER


def test_summary_verdict_field_takes_priority_over_legacy_inference() -> None:
    """4c 后 summary 顶层有 verdict 字段，应直接读取，不走老逻辑。"""
    # 故意把字段做成"按老逻辑会推 BETTER"，但顶层 verdict 是 SAME
    summary = {
        "verdict": "A/B 🟰",
        "safe_to_upgrade": True,    # 老逻辑会优先推 BETTER
        "rate_diff": 5.0,
        "regressions": 0,
    }
    assert verdict_from_ab_summary(summary) == ABVerdict.SAME


# ── compute_verdict ──


def test_compute_verdict_better_when_clearly_improved() -> None:
    assert compute_verdict(rate_diff=5.0, regressions=0, improvements=3, tolerance=1.0) == ABVerdict.BETTER


def test_compute_verdict_worse_when_any_regression() -> None:
    """有回归一律 WORSE，不管 rate_diff 多漂亮。"""
    assert compute_verdict(rate_diff=10.0, regressions=1, improvements=5, tolerance=1.0) == ABVerdict.WORSE


def test_compute_verdict_worse_when_rate_drops_beyond_tolerance() -> None:
    assert compute_verdict(rate_diff=-2.0, regressions=0, improvements=0, tolerance=1.0) == ABVerdict.WORSE


def test_compute_verdict_same_within_tolerance_band() -> None:
    """|rate_diff| ≤ tolerance 视为 SAME。"""
    assert compute_verdict(rate_diff=0.5, regressions=0, improvements=1, tolerance=1.0) == ABVerdict.SAME
    assert compute_verdict(rate_diff=-0.5, regressions=0, improvements=0, tolerance=1.0) == ABVerdict.SAME


def test_compute_verdict_same_when_net_improvement_insufficient() -> None:
    """rate_diff 突破阈值但改善数没多于回归数（极端情况），仍归 SAME。

    注：regressions=0 时 improvements>0 自动满足 improvements>regressions，
    所以这里测 improvements==regressions==0 但 rate_diff>tolerance 的边界。
    实际上这种情况只可能出现在 base/cand 用例集不完全重合的边角场景。
    """
    assert compute_verdict(rate_diff=2.0, regressions=0, improvements=0, tolerance=1.0) == ABVerdict.SAME


def test_compute_verdict_default_tolerance_zero() -> None:
    """tolerance 默认 0.0：任何正向变化都判 BETTER（与老 is_safe_to_upgrade 行为一致）。"""
    assert compute_verdict(rate_diff=0.1, regressions=0, improvements=1) == ABVerdict.BETTER


# ── aggregate_verdicts ──


def test_aggregate_verdicts_takes_worst() -> None:
    assert aggregate_verdicts([ABVerdict.BETTER, ABVerdict.SAME, ABVerdict.WORSE]) == ABVerdict.WORSE
    assert aggregate_verdicts([ABVerdict.BETTER, ABVerdict.BETTER]) == ABVerdict.BETTER
    assert aggregate_verdicts([ABVerdict.BETTER, ABVerdict.SAME]) == ABVerdict.SAME
    assert aggregate_verdicts([ABVerdict.SAME, ABVerdict.SAME]) == ABVerdict.SAME


def test_aggregate_verdicts_empty_returns_same() -> None:
    """空列表（如全部 dataset 跑失败）保守归 SAME，不阻断也不放行。"""
    assert aggregate_verdicts([]) == ABVerdict.SAME
