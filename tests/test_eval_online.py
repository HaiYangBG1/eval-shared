from __future__ import annotations

from datetime import datetime, timezone

from eval_shared.cli.eval_online import _online_dataset_name, _online_run_name


def test_online_dataset_name_follows_agent_online_temp_convention() -> None:
    """三层 dataset 架构约定：eval-online 工作区命名为 {agent}-online-temp。"""
    assert _online_dataset_name("intention") == "intention-online-temp"
    assert _online_dataset_name("recommend") == "recommend-online-temp"


def test_online_run_name_distinct_prefix_from_ab_runs() -> None:
    """eval-online 用 `online-` 前缀，避免和 A/B 的 `ab-baseline/ab-candidate` 撞 cache 查询。"""
    ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    name = _online_run_name("intention", "intention_pass", ts)
    assert name == "online-intention-intention_pass-20260511T120000Z"
    assert name.startswith("online-")
    assert not name.startswith("ab-")
