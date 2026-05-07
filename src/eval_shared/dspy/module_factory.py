"""
动态创建 DSPy Signature 和 Module。

根据 YAML 配置文件中的任务定义，自动构建对应的 DSPy 组件。
支持 Predict 和 ChainOfThought 两种模块模式。

description 来源（按优先级）：
  1. description_file: 读取 prompt.yaml 中的 system message（Single Source）
  2. description: 内联文本（会与 prompt.yaml 产生重复，不推荐）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval_shared.common.yaml_utils import load_yaml


def _extract_description_from_prompt_file(prompt_file: str) -> str:
    """
    从 prompt.yaml 中提取 system message 作为任务描述。

    prompt.yaml 格式为 PromptFoo 标准：
        - role: system
          content: |
            <system prompt content>
        - role: user
          content: '{{query}}'

    Args:
        prompt_file: prompt.yaml 文件路径

    Returns:
        system message 的文本内容
    """
    path = Path(prompt_file)
    if not path.exists():
        raise FileNotFoundError(f"description_file 不存在: {prompt_file}")

    data = load_yaml(path)
    if not data:
        raise ValueError(f"description_file 为空: {prompt_file}")

    # 支持两种格式：
    # 1. 列表格式（PromptFoo）：[{role: system, content: ...}, ...]
    # 2. 字符串格式：直接是文本
    if isinstance(data, str):
        return data

    if isinstance(data, list):
        for msg in data:
            if isinstance(msg, dict) and msg.get("role") == "system":
                return msg.get("content", "")

    if isinstance(data, dict) and "content" in data:
        return data["content"]

    raise ValueError(
        f"无法从 {prompt_file} 中提取 system message。"
        "期望格式: [{role: system, content: ...}]"
    )


def create_signature(task_config: dict) -> Any:
    """
    根据任务配置动态创建 DSPy Signature。

    description 来源：
      1. description_file → 从 prompt.yaml 提取 system message（推荐）
      2. description → 内联文本（不推荐，会与 prompt.yaml 重复）

    Args:
        task_config: YAML 配置中的 task 段，格式：
            {
                "description_file": "agents/intention/prompt.yaml",  # 推荐
                "description": "任务描述",  # 备选
                "input_fields": [{"name": "query", "desc": "..."}],
                "output_fields": [{"name": "answer", "desc": "..."}],
            }

    Returns:
        一个 dspy.Signature 子类
    """
    import dspy

    fields: dict[str, Any] = {}
    for f in task_config.get("input_fields", []):
        fields[f["name"]] = dspy.InputField(desc=f.get("desc", ""))
    for f in task_config.get("output_fields", []):
        fields[f["name"]] = dspy.OutputField(desc=f.get("desc", ""))

    # Single Source: 优先从 prompt 文件读取
    desc_file = task_config.get("description_file")
    if desc_file:
        description = _extract_description_from_prompt_file(desc_file)
    else:
        description = task_config.get("description", "完成指定任务。")

    # 使用 type() 动态创建 Signature 类
    sig = type(
        "TaskSignature",
        (dspy.Signature,),
        {"__doc__": description, **fields},
    )
    return sig


def create_module(signature: Any, module_type: str = "predict") -> Any:
    """
    根据 Signature 和模块类型创建 DSPy Module。

    Args:
        signature: dspy.Signature 类
        module_type: "predict" 或 "chain_of_thought"

    Returns:
        dspy.Module 实例
    """
    import dspy

    class GenericModule(dspy.Module):
        def __init__(self):
            super().__init__()
            if module_type == "chain_of_thought":
                self.predictor = dspy.ChainOfThought(signature)
            else:
                self.predictor = dspy.Predict(signature)

        def forward(self, **kwargs):
            return self.predictor(**kwargs)

    return GenericModule()


def get_field_names(task_config: dict) -> tuple[list[str], list[str]]:
    """
    从任务配置中提取输入/输出字段名。

    Returns:
        (input_field_names, output_field_names)
    """
    input_names = [f["name"] for f in task_config.get("input_fields", [])]
    output_names = [f["name"] for f in task_config.get("output_fields", [])]
    return input_names, output_names
