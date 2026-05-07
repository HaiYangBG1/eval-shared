"""YAML 读写工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> Any:
    """读取 YAML 文件并返回解析后的 Python 对象。"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def dump_yaml(data: Any, path: str | Path, header: str = "") -> None:
    """将 Python 对象写入 YAML 文件，可选添加文件头注释。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if header:
            f.write(header)
        yaml.dump(
            data,
            f,
            allow_unicode=True,
            default_flow_style=False,
            width=10000,
            sort_keys=False,
        )
