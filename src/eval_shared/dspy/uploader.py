"""
将 DSPy 优化结果上传到 Langfuse Prompt 管理。
"""

from __future__ import annotations

import click

from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


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
