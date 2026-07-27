"""PII 脱敏 v1 规则测试（契约 §2.3：只脱用户话术，不碰业务数据）。"""

from __future__ import annotations

from eval_shared.common.pii import scrub_text, scrub_user_content


# ── scrub_text：规则本体 ──


def test_mobile_number_becomes_phone() -> None:
    assert scrub_text("我手机13812345678，好了叫我") == "我手机<PHONE>，好了叫我"


def test_seven_plus_digits_become_phone() -> None:
    assert scrub_text("联系 8888888") == "联系 <PHONE>"


def test_short_digits_are_kept() -> None:
    # 业务语义（份数/桌数/6 位以下数字）不脱敏
    assert scrub_text("要3份米饭，123456") == "要3份米饭，123456"


def test_addressed_name_becomes_name() -> None:
    assert scrub_text("张先生要一份小炒肉") == "<NAME>要一份小炒肉"
    assert scrub_text("给李女士和王小姐上菜") == "给<NAME>和<NAME>上菜"


def test_labeled_ids_become_id() -> None:
    assert scrub_text("会员号 88888888 结账") == "会员号<ID> 结账"
    assert scrub_text("桌号：A12") == "桌号<ID>"
    assert scrub_text("12号桌加个菜") == "<ID>号桌加个菜"


def test_dish_names_untouched() -> None:
    assert scrub_text("来份农家小炒肉，微辣") == "来份农家小炒肉，微辣"


def test_quoted_json_business_numbers_protected() -> None:
    # Dify obs 的 user 消息整段是模板：菜单 JSON 嵌在 role=user content 里
    #（首迁 36 条实测 1277 处全是引号包裹的 food_id），必须护住
    menu = '[{"name":"农家蒸蛋","food_id":"1300000029","stock":983}]'
    assert scrub_text(menu) == menu
    # 护 JSON 的同时，同段里未加引号的口述手机号仍要脱
    mixed = '菜单 {"food_id":"1300000021"}，回电13812345678'
    assert scrub_text(mixed) == '菜单 {"food_id":"1300000021"}，回电<PHONE>'


# ── scrub_user_content：作用范围 ──


def test_messages_array_only_user_role_scrubbed() -> None:
    messages = [
        {"role": "system", "content": '菜单 {"food_id":"1300000021","stock":907}'},
        {"role": "user", "content": "订餐电话13812345678"},
        {"role": "assistant", "content": "回拨 13812345678 确认"},
    ]
    scrubbed, changes = scrub_user_content(messages)

    # system（菜单 food_id）与 assistant 均不碰——全量正则会把业务数字打烂
    assert scrubbed[0]["content"] == messages[0]["content"]
    assert scrubbed[2]["content"] == messages[2]["content"]
    assert scrubbed[1]["content"] == "订餐电话<PHONE>"
    assert changes == [("$[1].content", "订餐电话13812345678", "订餐电话<PHONE>")]
    # 原对象不被原地修改
    assert messages[1]["content"] == "订餐电话13812345678"


def test_dict_vars_only_query_scrubbed() -> None:
    vars_data = {
        "query": "张先生订桌，电话13812345678",
        "available_menu": '[{"food_id":"1300000029","sold":2976}]',
    }
    scrubbed, changes = scrub_user_content(vars_data)

    assert scrubbed["query"] == "<NAME>订桌，电话<PHONE>"
    assert scrubbed["available_menu"] == vars_data["available_menu"]
    assert len(changes) == 1 and changes[0][0] == "$.query"


def test_plain_string_scrubbed_whole() -> None:
    scrubbed, changes = scrub_user_content("会员号 9999999")
    assert scrubbed == "会员号<ID>"
    assert len(changes) == 1


def test_clean_content_reports_no_changes() -> None:
    scrubbed, changes = scrub_user_content([{"role": "user", "content": "来个招牌菜"}])
    assert scrubbed[0]["content"] == "来个招牌菜"
    assert changes == []


def test_non_str_values_pass_through() -> None:
    assert scrub_user_content(42) == (42, [])
    assert scrub_user_content(None) == (None, [])
