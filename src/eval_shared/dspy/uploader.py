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


def extract_prompt_from_module(optimized_module: Any) -> dict:
    """
    从优化后的 DSPy Module 中提取 Prompt 信息。

    解析 Module 的内部状态，提取：
    - instructions: 优化器找到的最佳指令文本
    - demos: 被选中的 Few-shot 示例

    Args:
        optimized_module: 优化后的 DSPy Module

    Returns:
        {
            "instructions": str,
            "demos": list[dict],
            "messages": list[dict],  # Langfuse chat 格式
        }
    """
    instructions = ""
    demos = []

    # 遍历 Module 中的所有 Predict 子模块
    for _name, predictor in optimized_module.named_predictors():
        # 提取 instructions
        if hasattr(predictor, "extended_signature"):
            sig = predictor.extended_signature
            if hasattr(sig, "instructions"):
                instructions = sig.instructions
            elif sig.__doc__:
                instructions = sig.__doc__
        elif hasattr(predictor, "signature"):
            sig = predictor.signature
            if hasattr(sig, "instructions"):
                instructions = sig.instructions
            elif sig.__doc__:
                instructions = sig.__doc__

        # 提取 demos（Few-shot 示例）
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
                demos.append(demo_dict)
        break  # 只取第一个 predictor

    # 构建 Langfuse chat messages 格式
    system_content = instructions
    if demos:
        system_content += "\n\n### 示例\n\n"
        for i, demo in enumerate(demos, 1):
            system_content += f"**示例 {i}:**\n"
            for k, v in demo.items():
                system_content += f"- {k}: {v}\n"
            system_content += "\n"

    messages = [
        {"role": "system", "content": system_content.strip()},
    ]

    return {
        "instructions": instructions,
        "demos": demos,
        "messages": messages,
    }


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
) -> dict:
    """
    从优化后的 Module 中提取 Prompt 并上传到 Langfuse。

    Args:
        optimized_module: 优化后的 DSPy Module
        prompt_name: Langfuse Prompt 名称
        label: 标签

    Returns:
        Langfuse API 返回的 prompt 对象
    """
    extracted = extract_prompt_from_module(optimized_module)
    click.echo(f"  提取到 instructions: {len(extracted['instructions'])} 字符")
    click.echo(f"  提取到 demos: {len(extracted['demos'])} 条")
    return upload_optimized_prompt(prompt_name, extracted["messages"], label)
