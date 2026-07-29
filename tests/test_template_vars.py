"""template_vars 解析器测试（契约 §2.3 regression vars 口径，#39 方案 A）。

样例消息取自 eval-ai-order 真实 obs 结构（2026-07-28 首迁 36 条的形态）：
[system, user(# Context Information 模板), user(原始 query)]。
"""

from __future__ import annotations

import pytest

from eval_shared.common.template_vars import (
    VarParseError,
    is_multi_turn,
    load_var_mapping,
    parse_obs_input,
)

_RECOMMEND_MAPPING = {
    "query_var": "query",
    "sections": {"Menu Data": "menu_data", "Rule Class": "rule_class"},
}

_RECOMMEND_CONTEXT = (
    "# Context Information\n\n"
    "## 1. [Menu Data] (门店菜单)\n\n"
    '[{"name":"农家小炒肉","food_id":"1300000021","stock":990}]\n\n'
    "## 2. [Rule Class] (当前任务规则)\n\n"
    "单人餐，推荐1个荤菜1个素菜1份米饭\n\n"
    "(注意：请严格遵守上述规则定义的菜品数量和类型)\n\n"
    "# Action\n\n"
    "请根据上述信息，执行 System Prompt 中的 Workflow，并返回 JSON 结果。"
)


def _messages(context: str = _RECOMMEND_CONTEXT, query: str = "来个招牌菜") -> list:
    return [
        {"role": "system", "content": "system prompt 原文"},
        {"role": "user", "files": [], "content": context},
        {"role": "user", "files": [], "content": query},
    ]


# ── 兜底路径 ──


def test_dict_input_passes_through() -> None:
    vars_ = {"query": "a", "menu_data": "[]"}
    assert parse_obs_input(vars_, None) == vars_


def test_str_input_becomes_query_var() -> None:
    assert parse_obs_input("来一份小炒肉", None) == {"query": "来一份小炒肉"}
    assert parse_obs_input("x", {"query_var": "q", "sections": {}}) == {"q": "x"}


def test_messages_without_mapping_raises() -> None:
    with pytest.raises(VarParseError, match="var-mapping.yaml"):
        parse_obs_input(_messages(), None)


def test_unparseable_type_raises() -> None:
    with pytest.raises(VarParseError):
        parse_obs_input(42, None)


# ── 消息数组解析 ──


def test_query_from_last_user_message() -> None:
    parsed = parse_obs_input(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "来一份小炒肉"}],
        {"query_var": "query", "sections": {}},
    )
    assert parsed == {"query": "来一份小炒肉"}


def test_sections_extracted_and_tail_note_stripped() -> None:
    parsed = parse_obs_input(_messages(), _RECOMMEND_MAPPING)
    assert parsed["query"] == "来个招牌菜"
    assert parsed["menu_data"] == '[{"name":"农家小炒肉","food_id":"1300000021","stock":990}]'
    # `(注意：…)` 是 Dify 模板尾巴，不是变量值；`# Action` 起属下一段
    assert parsed["rule_class"] == "单人餐，推荐1个荤菜1个素菜1份米饭"


def test_empty_section_is_legal_faithful_replay() -> None:
    """观测本来就空（07-28 实见 Menu Data 空段）→ 空字符串合法，忠实回放。"""
    context = (
        "# Context Information\n\n"
        "## 1. [Menu Data] (门店菜单)\n\n\n\n"
        "## 2. [Rule Class] (当前任务规则)\n\n单人餐\n"
    )
    parsed = parse_obs_input(_messages(context), _RECOMMEND_MAPPING)
    assert parsed["menu_data"] == ""
    assert parsed["rule_class"] == "单人餐"


def test_section_header_number_optional() -> None:
    context = "## [Rule Class]\n\n单人餐\n"
    mapping = {"query_var": "query", "sections": {"Rule Class": "rule_class"}}
    parsed = parse_obs_input(_messages(context), mapping)
    assert parsed["rule_class"] == "单人餐"


def test_missing_section_raises() -> None:
    context = "# Context Information\n\n## 1. [Menu Data]\n\n[]\n"
    with pytest.raises(VarParseError, match=r"\[Rule Class\]"):
        parse_obs_input(_messages(context), _RECOMMEND_MAPPING)


def test_last_user_message_being_context_raises() -> None:
    """末条 user 消息本身是上下文模板 → 观测缺独立原始输入，硬失败。"""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": _RECOMMEND_CONTEXT},
    ]
    with pytest.raises(VarParseError, match="原始用户输入"):
        parse_obs_input(msgs, _RECOMMEND_MAPPING)


def test_no_user_message_raises() -> None:
    with pytest.raises(VarParseError, match="user"):
        parse_obs_input([{"role": "system", "content": "s"}], _RECOMMEND_MAPPING)


# ── 映射文件加载 ──


def test_load_var_mapping_missing_returns_none(tmp_path) -> None:
    assert load_var_mapping("intention", base=tmp_path) is None


def test_load_var_mapping_defaults_and_sections(tmp_path) -> None:
    p = tmp_path / "agents" / "recommend" / "datasets"
    p.mkdir(parents=True)
    (p / "var-mapping.yaml").write_text(
        'sections:\n  "Menu Data": menu_data\n', encoding="utf-8"
    )
    mapping = load_var_mapping("recommend", base=tmp_path)
    assert mapping == {"query_var": "query", "sections": {"Menu Data": "menu_data"}}


def test_load_var_mapping_malformed_raises(tmp_path) -> None:
    p = tmp_path / "agents" / "recommend" / "datasets"
    p.mkdir(parents=True)
    (p / "var-mapping.yaml").write_text("sections:\n  - menu_data\n", encoding="utf-8")
    with pytest.raises(VarParseError, match="sections"):
        load_var_mapping("recommend", base=tmp_path)


def test_load_var_mapping_nonstandard_query_var_raises(tmp_path) -> None:
    """PII v1 只脱 dict vars 的 query 字段——query_var 改名会绕过脱敏，硬失败兜底。"""
    p = tmp_path / "agents" / "recommend" / "datasets"
    p.mkdir(parents=True)
    (p / "var-mapping.yaml").write_text("query_var: user_input\n", encoding="utf-8")
    with pytest.raises(VarParseError, match="PII"):
        load_var_mapping("recommend", base=tmp_path)


def test_last_user_context_detected_without_sections() -> None:
    """sections 为空（intention 类）时，用 `# Context Information` 抬头兜底识别 context 消息。"""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": _RECOMMEND_CONTEXT},
    ]
    with pytest.raises(VarParseError, match="原始用户输入"):
        parse_obs_input(msgs, {"query_var": "query", "sections": {}})


# ── 多轮观测标记（契约 §2.3 规则④）──


def test_single_turn_obs_is_not_multi_turn() -> None:
    assert not is_multi_turn(_messages())  # [system, user(context), user(query)]
    assert not is_multi_turn(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    )
    assert not is_multi_turn({"query": "dict 型不算"})


def test_assistant_turn_marks_multi_turn() -> None:
    assert is_multi_turn([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "推荐当季新品"},
        {"role": "assistant", "content": "好的，为您推荐…"},
        {"role": "user", "content": "这不好吃"},
    ])


def test_three_user_turns_marks_multi_turn() -> None:
    assert is_multi_turn([
        {"role": "user", "content": "# Context Information …"},
        {"role": "user", "content": "第一轮"},
        {"role": "user", "content": "第二轮"},
    ])
