from __future__ import annotations

import re
import uuid

from eval_shared.common.ingestion import (
    build_score_event,
    build_trace_event,
    new_trace_id,
)


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, TypeError):
        return False


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ── new_trace_id ──


def test_new_trace_id_returns_uuid_v4_string() -> None:
    tid = new_trace_id()
    assert _is_uuid(tid)
    assert uuid.UUID(tid).version == 4


def test_new_trace_id_uniqueness() -> None:
    """碰撞概率 ≈ 0，两次连续生成应不同。"""
    assert new_trace_id() != new_trace_id()


# ── build_trace_event ──


def test_build_trace_event_minimal() -> None:
    tid = new_trace_id()
    ev = build_trace_event(trace_id=tid, name="t1")

    assert ev["type"] == "trace-create"
    assert _is_uuid(ev["id"])           # envelope id
    assert ev["id"] != tid              # envelope id != body id
    assert _ISO_RE.match(ev["timestamp"])

    body = ev["body"]
    assert body["id"] == tid
    assert body["name"] == "t1"
    assert body["environment"] == "default"
    assert _ISO_RE.match(body["timestamp"])
    # 未提供的字段不应出现
    assert "input" not in body
    assert "output" not in body
    assert "metadata" not in body


def test_build_trace_event_with_optional_fields() -> None:
    ev = build_trace_event(
        trace_id="t-1",
        name="t1",
        input_data={"q": "hi"},
        output="hello",
        metadata={"v": 1},
        environment="staging",
    )
    body = ev["body"]
    assert body["input"] == {"q": "hi"}
    assert body["output"] == "hello"
    assert body["metadata"] == {"v": 1}
    assert body["environment"] == "staging"


# ── build_score_event ──


def test_build_score_event_uses_numeric_data_type_by_default() -> None:
    """约定：避开 BOOLEAN 坑，用 NUMERIC 0.0/1.0 表示 pass/fail。"""
    ev = build_score_event(trace_id="t-1", name="promptfoo_pass", value=1.0)

    assert ev["type"] == "score-create"
    body = ev["body"]
    assert body["traceId"] == "t-1"
    assert body["name"] == "promptfoo_pass"
    assert body["value"] == 1.0
    assert body["dataType"] == "NUMERIC"
    assert _is_uuid(body["id"])


def test_build_score_event_respects_explicit_score_id() -> None:
    ev = build_score_event(trace_id="t-1", name="x", value=0.0, score_id="custom-id")
    assert ev["body"]["id"] == "custom-id"


def test_build_score_event_with_comment() -> None:
    ev = build_score_event(
        trace_id="t-1", name="x", value=0.5, comment="from cache hit"
    )
    assert ev["body"]["comment"] == "from cache hit"


def test_build_score_event_envelope_id_is_uuid() -> None:
    """envelope id 用于 ingestion 去重；要确保自动生成且与 body.id 不同。"""
    ev = build_score_event(trace_id="t-1", name="x", value=1.0, score_id="custom-id")
    assert _is_uuid(ev["id"])
    assert ev["id"] != "custom-id"
