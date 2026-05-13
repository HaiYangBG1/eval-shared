"""
将 DSPy 优化结果上传到 Langfuse Prompt 管理。

支持两种方式：
  1. 直接上传 messages 列表
  2. 从优化后的 DSPy Module 中提取 Prompt 并上传
"""

from __future__ import annotations

import json
from typing import Any

import click

from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


def _extract_user_template_from_prompt_file(prompt_file: str) -> str | None:
    """
    从 prompt.yaml 中提取 user message 模板。

    prompt.yaml 格式为 PromptFoo 标准：
        - role: system
          content: |
            <system prompt content>
        - role: user
          content: |
            {{menu_data}}
            {{rule_class}}
            {{query}}

    Returns:
        user message 模板内容，未找到时返回 None
    """
    from eval_shared.common.yaml_utils import load_yaml
    from pathlib import Path

    path = Path(prompt_file)
    if not path.exists():
        return None

    data = load_yaml(path)
    if not isinstance(data, list):
        return None

    for msg in data:
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg.get("content", "")
    return None


def extract_prompt_from_module(
    optimized_module: Any,
    input_fields: list[str] | None = None,
    user_template_file: str | None = None,
) -> dict:
    """
    从优化后的 DSPy Module 中提取 Prompt 信息。

    解析 Module 的内部状态，提取：
    - instructions: 优化器找到的最佳指令文本
    - demos: 被选中的 Few-shot 示例

    Args:
        optimized_module: 优化后的 DSPy Module
        input_fields: 输入字段名列表（如 ["query"]），用于构建 user 消息模板。
                       若为 None 则尝试从 Signature 中自动推断。
        user_template_file: 原始 prompt.yaml 文件路径（如 agents/recommend/prompt.yaml）。
                            若提供，则从中读取 user 消息模板，保留原有多变量结构。

    Returns:
        {
            "instructions": str,
            "demos": list[dict],
            "messages": list[dict],  # Langfuse chat 格式（含 system + user）
        }
    """
    instructions = ""
    demos = []
    inferred_input_fields: list[str] = []

    # DSPy 内部元数据 key，不应出现在 few-shot demo 中
    _DSPY_META_KEYS = {"augmented", "dspy_uuid", "dspy_split"}

    # 遍历 Module 中的所有 Predict 子模块（不再 break）：
    #   - instructions 取第一个非空值（用户主 signature 通常是首个 predictor）
    #   - inferred_input_fields 也取第一个推断成功的结果
    #   - demos 聚合所有 predictor 的示例，避免 ChainOfThought 等多步模块丢数据
    for _name, predictor in optimized_module.named_predictors():
        sig = getattr(predictor, "extended_signature", None) or getattr(
            predictor, "signature", None
        )

        if sig is not None:
            sig_instructions = getattr(sig, "instructions", None) or sig.__doc__ or ""
            if sig_instructions and not instructions:
                instructions = sig_instructions

            if not input_fields and not inferred_input_fields:
                try:
                    inferred_input_fields = [
                        name
                        for name, field in sig.fields.items()
                        if hasattr(field, "json_schema_extra")
                        and field.json_schema_extra.get("__dspy_field_type") == "input"
                    ]
                except Exception:
                    pass

        if hasattr(predictor, "demos") and predictor.demos:
            for demo in predictor.demos:
                demo_dict = {}
                if hasattr(demo, "_store"):
                    demo_dict = dict(demo._store)
                elif hasattr(demo, "items"):
                    demo_dict = dict(demo.items())
                else:
                    demo_dict = {k: str(v) for k, v in demo.__dict__.items()
                                 if not k.startswith("_")}
                # 过滤掉 DSPy 内部元数据
                demo_dict = {
                    k: v for k, v in demo_dict.items() if k not in _DSPY_META_KEYS
                }
                demos.append(demo_dict)

    # 确定最终使用的输入字段列表
    effective_input_fields = input_fields or inferred_input_fields or ["query"]

    # 构建 Langfuse chat messages 格式
    system_content = instructions
    if demos:
        system_content += "\n\n### 示例\n\n"
        for i, demo in enumerate(demos, 1):
            system_content += f"**示例 {i}:**\n"
            # 输入部分
            system_content += "输入："
            input_parts = [str(demo.get(f, "")) for f in effective_input_fields if f in demo]
            system_content += "、".join(input_parts) if input_parts else "（无）"
            system_content += "\n"
            # 输出部分：以 JSON 格式呈现（匹配生产输出格式）
            output_data = {k: v for k, v in demo.items() if k not in effective_input_fields}
            if output_data:
                system_content += f"输出：{json.dumps(output_data, ensure_ascii=False)}\n"
            system_content += "\n"

    # 确定 user 消息模板
    # 优先使用原始 prompt.yaml 中的 user template（保留多变量结构）
    user_template = None
    if user_template_file:
        user_template = _extract_user_template_from_prompt_file(user_template_file)
    if not user_template:
        user_template = _build_user_template(effective_input_fields)

    messages = [
        {"role": "system", "content": system_content.strip()},
        # ⚠️ 必须包含 user 消息模板，否则 PromptFoo/Langfuse 使用此 prompt 时
        # 无法注入用户输入，导致模型收不到 query 而全部回退到 default。
        {"role": "user", "content": user_template},
    ]

    return {
        "instructions": instructions,
        "demos": demos,
        "messages": messages,
    }


def _build_user_template(input_fields: list[str]) -> str:
    """
    根据输入字段列表构建 user 消息模板。

    单字段（常见情况）：直接用 {{query}}
    多字段：拼接为 "字段1: {{字段1}}\\n字段2: {{字段2}}"
    """
    if len(input_fields) == 1:
        return "{{" + input_fields[0] + "}}"
    return "\n".join(f"{f}: {{{{{f}}}}}" for f in input_fields)


def upload_optimized_prompt(
    prompt_name: str,
    messages: list[dict],
    label: str = "staging",
) -> dict:
    """
    将优化后的 Prompt 上传到 Langfuse 并标记指定 label。

    Args:
        prompt_name: Langfuse Prompt 名称（如 "intention-prompt"）
        messages: chat 格式消息列表 [{role, content}, ...]
        label: 标签（默认 staging）

    Returns:
        Langfuse API 返回的 prompt 对象
    """
    init_env()
    with LangfuseClient() as client:
        body = {
            "name": prompt_name,
            "type": "chat",
            "prompt": messages,
            "labels": [label],
        }
        result = client.create_prompt(body)

    click.echo(
        f"✅ 已上传优化后的 Prompt → {prompt_name} "
        f"v{result.get('version')} (label={label})"
    )
    return result


def upload_from_module(
    optimized_module: Any,
    prompt_name: str,
    label: str = "staging",
    input_fields: list[str] | None = None,
    user_template_file: str | None = None,
) -> dict:
    """
    从优化后的 Module 中提取 Prompt 并上传到 Langfuse。

    Args:
        optimized_module: 优化后的 DSPy Module
        prompt_name: Langfuse Prompt 名称
        label: 标签
        input_fields: 输入字段名列表（传递给 extract_prompt_from_module）
        user_template_file: 原始 prompt.yaml 路径（保留多变量 user 模板）

    Returns:
        Langfuse API 返回的 prompt 对象
    """
    extracted = extract_prompt_from_module(
        optimized_module,
        input_fields=input_fields,
        user_template_file=user_template_file,
    )
    click.echo(f"  提取到 instructions: {len(extracted['instructions'])} 字符")
    click.echo(f"  提取到 demos: {len(extracted['demos'])} 条")
    click.echo(f"  user 消息模板: {extracted['messages'][-1]['content']}")
    return upload_optimized_prompt(prompt_name, extracted["messages"], label)
