"""
从 Langfuse 或本地 JSON 加载 DSPy Examples。

支持两种数据来源：
  1. 直接从 Langfuse Dataset API 加载
  2. 从 eval-export-dspy 导出的 JSON 文件加载

支持单字段和多字段任务配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_from_json(
    path: str | Path,
    input_field: str = "query",
    output_field: str = "answer",
) -> list[Any]:
    """
    从 eval-export-dspy 导出的 JSON 文件加载为 dspy.Example 列表。

    Args:
        path: JSON 文件路径
        input_field: 输入字段名（默认 query）
        output_field: 输出字段名（默认 answer）

    Returns:
        list[dspy.Example]
    """
    try:
        import dspy
    except ImportError:
        raise ImportError(
            "dspy 未安装，请运行: pip install eval-shared[dspy]"
        )

    data = json.loads(Path(path).read_text("utf-8"))
    examples = []
    for item in data:
        ex = dspy.Example(
            **{input_field: item.get(input_field, ""), output_field: item.get(output_field, "")}
        ).with_inputs(input_field)
        examples.append(ex)

    return examples


def load_from_langfuse(
    dataset_name: str,
    input_field: str = "query",
    output_field: str = "answer",
    input_fields: list[str] | None = None,
    output_fields: list[str] | None = None,
) -> list[Any]:
    """
    直接从 Langfuse Dataset API 加载为 dspy.Example 列表。

    支持两种模式：
      - 单字段（默认）：通过 input_field/output_field 兼容旧配置
      - 多字段：通过 input_fields/output_fields 列表支持多变量任务

    Args:
        dataset_name: Langfuse 上的 dataset 名称
        input_field: 输入字段名（单字段模式，从 input dict 中提取）
        output_field: 输出字段名（单字段模式，从 expectedOutput 提取）
        input_fields: 输入字段名列表（多字段模式，优先于 input_field）
        output_fields: 输出字段名列表（多字段模式，优先于 output_field）

    Returns:
        list[dspy.Example]
    """
    try:
        import dspy
    except ImportError:
        raise ImportError(
            "dspy 未安装，请运行: pip install eval-shared[dspy]"
        )

    from eval_shared.common.config import init_env
    from eval_shared.common.langfuse_client import LangfuseClient

    init_env()
    with LangfuseClient() as client:
        items = client.get_dataset_items(dataset_name)

    # 确定实际使用的字段列表
    in_fields = input_fields or [input_field]
    out_fields = output_fields or [output_field]
    is_multi_input = len(in_fields) > 1

    examples = []
    for item in items:
        input_data = item.get("input", {})
        expected = item.get("expectedOutput")

        # ── 构建输入字段 ──
        fields: dict[str, str] = {}
        if is_multi_input:
            # 多字段模式：从 input dict 中逐一提取
            for f in in_fields:
                val = input_data.get(f, "")
                fields[f] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        else:
            # 单字段模式（向后兼容）
            fields[in_fields[0]] = (
                input_data.get(in_fields[0])
                or input_data.get("question")
                or json.dumps(input_data, ensure_ascii=False)
            )

        # ── 构建输出字段 ──
        for f in out_fields:
            if isinstance(expected, dict) and f in expected:
                raw = expected[f]
                fields[f] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            elif isinstance(expected, str):
                fields[f] = expected
            elif expected:
                fields[f] = json.dumps(expected, ensure_ascii=False)
            else:
                fields[f] = ""

        ex = dspy.Example(**fields).with_inputs(*in_fields)
        examples.append(ex)

    return examples


def load_from_json_multi(
    path: str | Path,
    input_fields: list[str],
    output_fields: list[str],
) -> list[Any]:
    """
    从 JSON 文件加载为多字段 dspy.Example 列表。

    适用于任务有多个输入/输出字段的情况。

    Args:
        path: JSON 文件路径
        input_fields: 输入字段名列表
        output_fields: 输出字段名列表

    Returns:
        list[dspy.Example]
    """
    try:
        import dspy
    except ImportError:
        raise ImportError(
            "dspy 未安装，请运行: pip install eval-shared[dspy]"
        )

    data = json.loads(Path(path).read_text("utf-8"))
    examples = []
    for item in data:
        fields = {}
        for f in input_fields:
            fields[f] = item.get(f, "")
        for f in output_fields:
            fields[f] = item.get(f, "")
        ex = dspy.Example(**fields).with_inputs(*input_fields)
        examples.append(ex)

    return examples
