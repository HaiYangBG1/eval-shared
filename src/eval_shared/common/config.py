"""
环境变量加载与校验。

模型分层策略（能力梯度递增）：
  - 被测模型 (DASHSCOPE_*) — 成本优先
  - 评分模型 (OPENAI_* / PROMPTFOO_*) — 判断力优先
  - 线上评估模型 (EVAL_MODEL_*) — 回退到 DASHSCOPE_*
  - DSPy 优化器 (DSPY_LM_*) — 质量优先，须 ≥ 评分模型

其他逻辑：
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
            "ssl_verify": bool,   # LANGFUSE_SSL_VERIFY=false 时为 False（自签/IP 证书场景）
        }
    """
    import base64

    base_url = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or ""
    ).rstrip("/")
    ssl_verify = os.environ.get("LANGFUSE_SSL_VERIFY", "true").strip().lower() not in (
        "false", "0", "no",
    )

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
        "ssl_verify": ssl_verify,
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


def get_dspy_lm_config() -> dict:
    """
    返回 DSPy LM 配置。

    DSPy 优化器作为「教师」角色，应使用比被测模型更强的模型。
    三个变量均独立配置，支持回退以便快速启动。

    读取顺序：
      DSPY_LM_MODEL    → 必须（如 openai/<model-name>）
      DSPY_LM_API_BASE → 回退到 EVAL_MODEL_BASE_URL → DASHSCOPE_BASE_URL
      DSPY_LM_API_KEY  → 回退到 EVAL_MODEL_API_KEY  → DASHSCOPE_API_KEY

    Returns:
        {
            "model": str,        # DSPy model identifier
            "api_base": str,
            "api_key": str,
        }
    """
    model = os.environ.get("DSPY_LM_MODEL", "")
    if not model:
        print("❌ 缺少环境变量：DSPY_LM_MODEL（如 openai/<model-name>）", file=sys.stderr)
        sys.exit(1)

    api_base = (
        os.environ.get("DSPY_LM_API_BASE")
        or os.environ.get("EVAL_MODEL_BASE_URL")
        or os.environ.get("DASHSCOPE_BASE_URL")
        or ""
    )
    api_key = (
        os.environ.get("DSPY_LM_API_KEY")
        or os.environ.get("EVAL_MODEL_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or ""
    )

    if not api_base or not api_key:
        print(
            "❌ 缺少 DSPy LM 配置：DSPY_LM_API_BASE / DSPY_LM_API_KEY",
            file=sys.stderr,
        )
        sys.exit(1)

    return {
        "model": model,
        "api_base": api_base.rstrip("/"),
        "api_key": api_key,
    }
