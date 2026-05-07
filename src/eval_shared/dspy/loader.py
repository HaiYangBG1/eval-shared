"""
从 Langfuse 或本地 JSON 加载 DSPy Examples。

支持两种数据来源：
  1. 直接从 Langfuse Dataset API 加载
  2. 从 eval-export-dspy 导出的 JSON 文件加载
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
) -> list[Any]:
    """
    直接从 Langfuse Dataset API 加载为 dspy.Example 列表。

    Args:
        dataset_name: Langfuse 上的 dataset 名称
        input_field: 输入字段名（从 input dict 中提取）
        output_field: 输出字段名（从 expectedOutput 提取）

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
        items = client.get_dataset_items(dataset_name, limit=200)

    examples = []
    for item in items:
        input_data = item.get("input", {})
        expected = item.get("expectedOutput")

        input_val = (
            input_data.get(input_field)
            or input_data.get("question")
            or json.dumps(input_data, ensure_ascii=False)
        )
        output_val = (
            expected if isinstance(expected, str)
            else json.dumps(expected, ensure_ascii=False) if expected
            else ""
        )

        ex = dspy.Example(
            **{input_field: input_val, output_field: output_val}
        ).with_inputs(input_field)
        examples.append(ex)

    return examples
