"""
动态创建 DSPy Signature 和 Module。

根据 YAML 配置文件中的任务定义，自动构建对应的 DSPy 组件。
支持 Predict 和 ChainOfThought 两种模块模式。
"""

from __future__ import annotations

from typing import Any


def create_signature(task_config: dict) -> Any:
    """
    根据任务配置动态创建 DSPy Signature。

    Args:
        task_config: YAML 配置中的 task 段，格式：
            {
                "description": "任务描述",
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
