from __future__ import annotations

from eval_shared.cli.dspy_pipeline import (
    _extract_agent_from_config,
    _generate_pipeline_report,
)


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
    ab_report = tmp_path / "agent-ab-report.md"
    ab_report.write_text(
        "## 🔴 回归 (1 个用例)\n\n"
        "## 结论\n\n"
        "✅ **安全升级**：历史报告中的旧口径。\n",
        encoding="utf-8",
    )

    report = _generate_pipeline_report(
        "agent",
        {"delta": 0.5},
        str(ab_report),
        skipped_ab=False,
    )

    assert "### ⚠️ 建议：暂缓升级" in report
    assert "### ✅ 建议：执行升级" not in report
