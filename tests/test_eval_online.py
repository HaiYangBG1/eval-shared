from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from eval_shared.cli.eval_online import (
    _load_watermark,
    _online_dataset_name,
    _online_run_name,
    _resolve_since,
    _save_watermark,
)


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


NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_resolve_since_without_watermark_uses_hours_window() -> None:
    since, extended = _resolve_since(NOW, 24, None)
    assert since == NOW - timedelta(hours=24)
    assert extended is False


def test_resolve_since_extends_window_to_stale_watermark() -> None:
    """漏跑保护：上次运行点早于 --hours 窗口起点时，窗口扩展到上次运行点。"""
    watermark = NOW - timedelta(days=10)
    since, extended = _resolve_since(NOW, 24, watermark)
    assert since == watermark
    assert extended is True


def test_resolve_since_ignores_recent_watermark() -> None:
    """上次运行点在窗口内时维持 --hours 窗口（重扫由已有分数去重兜底）。"""
    watermark = NOW - timedelta(hours=2)
    since, extended = _resolve_since(NOW, 24, watermark)
    assert since == NOW - timedelta(hours=24)
    assert extended is False


def test_watermark_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / ".eval-online-state.json"
    _save_watermark(p, NOW)
    assert _load_watermark(p) == NOW


def test_load_watermark_missing_or_corrupt_returns_none(tmp_path: Path) -> None:
    assert _load_watermark(tmp_path / "nope.json") is None
    p = tmp_path / "bad.json"
    p.write_text("{not json", "utf-8")
    assert _load_watermark(p) is None
