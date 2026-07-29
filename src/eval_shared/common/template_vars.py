"""observation input → promptfoo 模板变量解析（契约 §2.3 regression vars 口径，#39 方案 A）。

regression 条目的 vars 必须是 promptfoo 可直接消费的 dict 型模板变量（与 golden 同构）；
Dify obs 的 messages 数组在 promote 时按 per-agent 映射配置解析成 dict。
映射配置在业务仓 `agents/<agent>/datasets/var-mapping.yaml`，本模块只实现通用
解析器，不内置任何 agent 名（eval-shared 核心原则）。

解析规则（契约 §2.3）：
  - query_var ← 末条 role=user 消息 content（Dify 观测末条 user 消息即原始用户输入）；
    该消息若本身含配置的段落标题（观测缺独立原始输入）→ 硬失败
  - 每个配置的段落标题，取 `## n. [标题]` 行到下一个 `#` 打头行之间的文本，
    strip 后剥离尾部 `(注意：…)` 模板行（Dify 模板尾巴，不是变量值）
  - system 消息丢弃（A/B 候选 prompt 由本地 prompt.yaml 模板注入）
  - 段落缺失 → 硬失败（不写半程、不猜测）；段落内容为空 = 合法（忠实回放）
"""

from __future__ import annotations

import re
from pathlib import Path

from eval_shared.common.yaml_utils import load_yaml

DEFAULT_QUERY_VAR = "query"

# 模板尾巴行：`(注意：…)` 半/全角括号都认，只从段尾剥离
_TAIL_NOTE_RE = re.compile(r"[（(]注意[:：][^)）]*[)）]\s*$")


class VarParseError(ValueError):
    """observation input 无法按映射配置解析为模板变量（契约 §2.3 要求硬失败）。"""


def var_mapping_path(agent: str, base: Path | None = None) -> Path:
    return (base or Path.cwd()) / "agents" / agent / "datasets" / "var-mapping.yaml"


def load_var_mapping(agent: str, base: Path | None = None) -> dict | None:
    """读 per-agent 映射配置；文件不存在返回 None（是否硬失败由调用方按 input 型决定）。"""
    path = var_mapping_path(agent, base)
    if not path.exists():
        return None
    data = load_yaml(path) or {}
    if not isinstance(data, dict):
        raise VarParseError(f"映射文件格式错误（应为 dict）：{path}")
    sections = data.get("sections") or {}
    if not isinstance(sections, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in sections.items()
    ):
        raise VarParseError(f"映射文件 sections 应为 {{段落标题: 变量名}}：{path}")
    query_var = data.get("query_var") or DEFAULT_QUERY_VAR
    if query_var != DEFAULT_QUERY_VAR:
        # PII v1（契约 §2.3）dict 型 vars 只脱 `query` 字段——query_var 改名会让
        # 用户话术绕过脱敏裸奔入 git。扩 pii.py 覆盖范围前，这里硬失败兜底
        raise VarParseError(
            f"query_var 当前仅支持 '{DEFAULT_QUERY_VAR}'（PII 脱敏范围钉死，"
            f"契约 §2.3）：{path}"
        )
    return {
        "query_var": query_var,
        "sections": sections,
    }


def is_multi_turn(value: object) -> bool:
    """消息数组是否为多轮观测（契约 §2.3 规则④：历史轮丢弃需显式标记+清单过目）。

    单轮 Dify obs 形态 = [system, user(context), user(query)]（≤2 条 user、无 assistant）。
    """
    if not isinstance(value, list):
        return False
    roles = [m.get("role") for m in value if isinstance(m, dict)]
    return "assistant" in roles or roles.count("user") > 2


def _section_header_re(title: str) -> re.Pattern[str]:
    # 匹配 `## 1. [Menu Data] (门店菜单)` 一类的段落标题行；序号可缺省
    return re.compile(
        rf"^##\s*(?:\d+\s*[.、]?\s*)?\[{re.escape(title)}\].*$", re.MULTILINE
    )


def _extract_section(content: str, title: str) -> str | None:
    """取段落标题行到下一个 `#` 打头行之间的文本；标题缺失返回 None。"""
    m = _section_header_re(title).search(content)
    if not m:
        return None
    rest = content[m.end():]
    next_header = re.search(r"^#", rest, re.MULTILINE)
    if next_header:
        rest = rest[: next_header.start()]
    return _TAIL_NOTE_RE.sub("", rest.strip()).strip()


def parse_obs_input(value: object, mapping: dict | None) -> dict:
    """observation input → dict 型模板变量（契约 §2.3 regression vars 口径）。

    - dict → 原样透传（已是模板变量，幂等）
    - str  → {query_var: 值}
    - 消息数组 → 按映射配置解析；mapping 缺失时硬失败
    """
    if isinstance(value, dict):
        return value

    query_var = (mapping or {}).get("query_var") or DEFAULT_QUERY_VAR
    sections: dict[str, str] = (mapping or {}).get("sections") or {}

    if isinstance(value, str):
        return {query_var: value}

    if not isinstance(value, list):
        raise VarParseError(f"无法解析的 input 类型：{type(value).__name__}")

    if mapping is None:
        raise VarParseError(
            "input 是消息数组但缺少映射配置 "
            "agents/<agent>/datasets/var-mapping.yaml（契约 §2.3）"
        )

    user_contents = [
        str(m.get("content") or "")
        for m in value
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    if not user_contents:
        raise VarParseError("消息数组中没有 role=user 消息")

    last = user_contents[-1].strip()
    # sections 为空（intention 类）时配置标题护栏不生效，
    # 用 Dify 模板固定抬头兜底识别 context 消息（契约 §2.3 规则①）
    if last.startswith("# Context Information") or any(
        _section_header_re(t).search(last) for t in sections
    ):
        raise VarParseError(
            "末条 user 消息本身是上下文模板（含段落标题），观测缺独立的原始用户输入"
        )
    parsed: dict[str, object] = {query_var: last}

    for title, var_name in sections.items():
        extracted = None
        for content in user_contents:
            extracted = _extract_section(content, title)
            if extracted is not None:
                break
        if extracted is None:
            raise VarParseError(f"段落 [{title}] 在消息数组中缺失")
        parsed[var_name] = extracted

    return parsed
