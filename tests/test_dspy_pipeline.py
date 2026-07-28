from __future__ import annotations

from eval_shared.cli.dspy_pipeline import (
    _extract_agent_from_config,
    _generate_pipeline_report,
)
from eval_shared.common.ab_verdict import ABVerdict


def test_extract_agent_prefers_prompt_name_over_dataset() -> None:
    config = {
        "dataset": "intention-golden",
        "output": {"prompt_name": "intention-prompt"},
    }

    assert _extract_agent_from_config(config) == "intention"


def test_extract_agent_strips_three_layer_dataset_suffixes_without_prompt() -> None:
    assert _extract_agent_from_config({"dataset": "recommend-golden"}) == "recommend"
    assert _extract_agent_from_config({"dataset": "recommend-regression"}) == "recommend"
    assert _extract_agent_from_config({"dataset": "recommend-online-temp"}) == "recommend"


def test_extract_agent_falls_back_to_dataset_for_legacy_config() -> None:
    assert _extract_agent_from_config({"dataset": "legacy-agent"}) == "legacy-agent"


def test_pipeline_report_treats_regression_as_blocker(tmp_path) -> None:
    # 报告文案与结构化结论矛盾时（历史旧口径），Part 3 必须以 ab_verdict 为准
    ab_report = tmp_path / "agent-ab-report.md"
    ab_report.write_text(
        "## 结论\n\n✅ **安全升级**：历史报告中的旧口径文案。\n",
        encoding="utf-8",
    )

    report = _generate_pipeline_report(
        "agent",
        {"delta": 0.5},
        str(ab_report),
        skipped_ab=False,
        ab_verdict=ABVerdict.WORSE,
    )

    assert "### ⚠️ 建议：暂缓升级" in report
    assert "### ✅ 建议：执行升级" not in report


def test_pipeline_report_better_verdict_recommends_upgrade(tmp_path) -> None:
    ab_report = tmp_path / "agent-ab-report.md"
    ab_report.write_text("## 结论\n", encoding="utf-8")

    report = _generate_pipeline_report(
        "agent",
        {"delta": 0.02},
        str(ab_report),
        skipped_ab=False,
        ab_verdict=ABVerdict.BETTER,
    )

    assert "### ✅ 建议：执行升级" in report
    assert "npm run promote -- --agent agent" in report


def test_pipeline_report_same_verdict_gets_explicit_branch(tmp_path) -> None:
    # SAME 不再落入含糊的"人工审核"兜底，而是有明确的 🟰 分支（07-21 审查 P2）
    ab_report = tmp_path / "agent-ab-report.md"
    ab_report.write_text("## 结论\n", encoding="utf-8")

    report = _generate_pipeline_report(
        "agent",
        {"delta": 0.02},
        str(ab_report),
        skipped_ab=False,
        ab_verdict=ABVerdict.SAME,
    )

    assert "### 🟰 建议：人工决策（候选与基线相当）" in report
    assert "### ✅ 建议：执行升级" not in report


def test_pipeline_report_missing_verdict_reports_insufficient_info(tmp_path) -> None:
    ab_report = tmp_path / "agent-ab-report.md"
    ab_report.write_text("## 结论\n", encoding="utf-8")

    report = _generate_pipeline_report(
        "agent",
        {"delta": 0.02},
        str(ab_report),
        skipped_ab=False,
        ab_verdict=None,
    )

    assert "### 🤔 建议：信息不足" in report
