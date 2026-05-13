"""Langfuse ingestion 事件构造 helper。

封装 ingestion API 的事件 envelope 格式，避免 promptfoo_ab / eval_online 各自手搓。

事件格式（来自 Langfuse OpenAPI spec）：
  {
    "id": "<envelope-uuid>",        # event 信封 id（用于 ingestion 去重）
    "timestamp": "ISO8601",
    "type": "trace-create" | "score-create" | ...,
    "body": { "id": "<trace-or-score-uuid>", ...其他字段 }
  }

⚠️ 4d.5 spike 实测发现 body.id 重复时是 first-write-wins，不是 spec 说的 upsert。
   每次构造事件都生成新的 UUID，不要复用。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_trace_event(
    *,
    trace_id: str,
    name: str,
    input_data: object | None = None,
    output: object | None = None,
    metadata: dict | None = None,
    environment: str = "default",
) -> dict:
    """构造一条 trace-create 事件。

    Args:
        trace_id: 客户端自生成的 UUID v4，作为 trace.body.id
        name: trace 显示名（如 `promptfoo-ab-baseline-{agent}`）
        input_data: 输入（任意 JSON 可序列化对象）
        output: 输出
        metadata: trace metadata
        environment: 环境标识，默认 "default"
    """
    body: dict[str, object] = {
        "id": trace_id,
        "timestamp": _iso_now(),
        "name": name,
        "environment": environment,
    }
    if input_data is not None:
        body["input"] = input_data
    if output is not None:
        body["output"] = output
    if metadata is not None:
        body["metadata"] = metadata

    return {
        "id": str(uuid.uuid4()),         # event envelope id（与 body.id 不同）
        "timestamp": _iso_now(),
        "type": "trace-create",
        "body": body,
    }


def build_score_event(
    *,
    trace_id: str,
    name: str,
    value: float,
    score_id: str | None = None,
    data_type: str = "NUMERIC",
    comment: str | None = None,
) -> dict:
    """构造一条 score-create 事件。

    Args:
        trace_id: 关联的 trace.id
        name: score 名（约定使用 `promptfoo_pass`）
        value: score 数值（NUMERIC 0.0/1.0；BOOLEAN 须用 0/1，但 BUGFIXES.md #5
            提过 BOOLEAN 写回 422 问题，约定用 NUMERIC 0.0/1.0 二值表示 pass/fail）
        score_id: 不传则自生成 UUID
        data_type: 默认 NUMERIC（避开 BOOLEAN 坑）
        comment: 可选备注
    """
    body: dict[str, object] = {
        "id": score_id or str(uuid.uuid4()),
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": data_type,
    }
    if comment is not None:
        body["comment"] = comment

    return {
        "id": str(uuid.uuid4()),
        "timestamp": _iso_now(),
        "type": "score-create",
        "body": body,
    }


def new_trace_id() -> str:
    """约定的 trace_id 生成方式：UUID v4 hex 字符串。"""
    return str(uuid.uuid4())
