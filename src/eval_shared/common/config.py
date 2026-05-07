"""
环境变量加载与校验。

逻辑与原 JS 版完全对等：
  - LANGFUSE_HOST 优先于 LANGFUSE_BASE_URL
  - EVAL_MODEL_* 未设时回退到 DASHSCOPE_*
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def init_env(env_path: str | Path | None = None) -> None:
    """加载 .env 文件到 os.environ（已有变量不覆盖）。"""
    if env_path is None:
        env_path = Path.cwd() / ".env"
    else:
        env_path = Path(env_path)

    if env_path.exists():
        load_dotenv(env_path, override=False)


def require_env(*names: str) -> None:
    """校验必需环境变量，缺失时打印错误并退出。"""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        for n in missing:
            print(f"❌ 缺少环境变量：{n}", file=sys.stderr)
        sys.exit(1)


def get_langfuse_config() -> dict:
    """
    返回 Langfuse 连接配置。

    Returns:
        {
            "base_url": str,      # 不带尾部斜杠
            "public_key": str,
            "secret_key": str,
            "auth_header": str,   # Base64 Basic auth
        }
    """
    import base64

    base_url = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or ""
    ).rstrip("/")

    if not base_url:
        print("❌ 缺少环境变量：LANGFUSE_HOST 或 LANGFUSE_BASE_URL", file=sys.stderr)
        sys.exit(1)

    require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]
    auth_header = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    return {
        "base_url": base_url,
        "public_key": public_key,
        "secret_key": secret_key,
        "auth_header": auth_header,
    }


def get_eval_model_config() -> dict:
    """
    返回评估模型（LLM Judge）配置。

    回退逻辑：
      EVAL_MODEL_BASE_URL → DASHSCOPE_BASE_URL
      EVAL_MODEL_API_KEY  → DASHSCOPE_API_KEY
      EVAL_MODEL_NAME     → 默认 qwen-plus

    Returns:
        {
            "base_url": str,
            "api_key": str,
            "model_name": str,
        }
    """
    base_url = (
        os.environ.get("EVAL_MODEL_BASE_URL")
        or os.environ.get("DASHSCOPE_BASE_URL")
        or ""
    )
    api_key = (
        os.environ.get("EVAL_MODEL_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or ""
    )
    model_name = os.environ.get("EVAL_MODEL_NAME", "qwen-plus")

    if not base_url or not api_key:
        print(
            "❌ 缺少评估模型配置：EVAL_MODEL_BASE_URL / EVAL_MODEL_API_KEY",
            file=sys.stderr,
        )
        sys.exit(1)

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model_name": model_name,
    }
