"""PII 脱敏 v1 —— regression 数据入 git 前的最低限度脱敏（契约 §2.3）。

只管「入 git」这一个执行点；Judge 链路（百炼）不脱敏（同信任域，2026-07-28 拍板）。

执行范围 = 用户话术字段：messages 数组中 `role=user` 的 content、dict 型 vars 的
`query` 字段。菜单/输出 JSON（food_id、sold、stock 等业务数字）与审计 metadata
一律不碰——全量正则会把 `"food_id":"1300000021"` 当成手机号打烂。
规则从轻（点餐场景实测 PII 风险低），漏脱由工具输出的 diff 人工过目兜底。
"""

from __future__ import annotations

import re
from typing import Any

# 常见单字姓白名单——剔除「和/于/时/方/安/成/万/白/高/文/武/石/金」等日常用字，
# 避免把「给李女士**和**王小姐」的连词或菜名字吃进 <NAME>
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华魏陶姜戚谢邹喻柏窦章"
    "苏潘葛奚范彭郎鲁韦昌马苗凤俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬乐傅皮卞"
    "齐康伍元卜顾孟平黄穆萧尹姚邵汪祁毛狄米贝臧伏戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵"
    "席季贾路娄江童颜郭梅盛林刁钟徐邱骆夏蔡田樊胡凌霍支柯卢莫房缪解宗丁宣邓单杭洪"
    "包诸左崔吉龚程邢裴陆荣翁甄曲封储靳段巫焦巴牧隗谷车侯蓬全班仰秋仲伊宫宁栾甘厉"
    "戎祖符刘景詹龙叶幸韶郜黎薄印宿怀蒲邰鄂索咸籍赖卓蔺屠蒙池乔阴胥苍闻莘党翟谭贡"
    "劳姬申扶堵冉宰郦雍璩桑桂濮牛寿边扈燕冀浦尚农温别庄晏柴瞿阎充慕连茹宦艾鱼容向"
    "古易慎戈廖庾终暨居衡步都耿满弘匡国寇广禄阙欧沃蔚越夔隆师巩聂晁勾敖融冷辛阚那"
    "简饶曾沙养鞠须丰关蒯相查后荆红游竺权逯盖益桓公"
)

# 顺序敏感：带标签的号码先行（「会员号 88888888」→ 会员号<ID> 而非 会员号<PHONE>）
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(桌号|会员号|工号|卡号|房间号)\s*[:：]?\s*[A-Za-z0-9-]{1,20}"), r"\1<ID>"),
    (re.compile(r"\d{1,4}号桌"), "<ID>号桌"),
    (re.compile(rf"[{_SURNAMES}][一-龥]{{0,2}}(先生|女士|小姐)"), "<NAME>"),
    (re.compile(r"\d{7,}"), "<PHONE>"),
]


# JSON 引号包裹的长数字 = 业务字段（"food_id":"1300000021"）。Dify obs 的 user 消息
# 常整段就是模板（菜单 JSON 嵌在 role=user 的 content 里，首迁 36 条实测 1277 处
# 全是这种），必须护住；用户口述手机号不会带 ASCII 双引号，不受影响。
_QUOTED_LONG_NUM = re.compile(r'"\d{7,}"')


def scrub_text(text: str) -> str:
    """对单段用户话术执行 v1 脱敏规则（JSON 引号包裹的业务数字先护住）。"""
    protected: dict[str, str] = {}

    def _mask(m: re.Match[str]) -> str:
        key = f"\x00P{len(protected)}\x00"
        protected[key] = m.group(0)
        return key

    text = _QUOTED_LONG_NUM.sub(_mask, text)
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    for key, original in protected.items():
        text = text.replace(key, original)
    return text


def scrub_user_content(value: Any) -> tuple[Any, list[tuple[str, str, str]]]:
    """只脱敏用户话术字段，返回 (脱敏后副本, 变更清单 [(路径, 原文, 脱敏后)])。

    - list 型（Dify obs 的 messages 数组）：仅 `role == "user"` 的 str content
    - dict 型 vars：仅 `query` 字段
    - 纯 str：整段按用户话术处理
    其余结构（菜单 JSON、expectedOutput、metadata）原样返回，不做任何替换。
    """
    changes: list[tuple[str, str, str]] = []

    def _scrub(text: str, path: str) -> str:
        scrubbed = scrub_text(text)
        if scrubbed != text:
            changes.append((path, text, scrubbed))
        return scrubbed

    if isinstance(value, str):
        return _scrub(value, "$"), changes

    if isinstance(value, list):
        out_list = []
        for i, msg in enumerate(value):
            if (
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
            ):
                msg = {**msg, "content": _scrub(msg["content"], f"$[{i}].content")}
            out_list.append(msg)
        return out_list, changes

    if isinstance(value, dict):
        out_dict = dict(value)
        if isinstance(out_dict.get("query"), str):
            out_dict["query"] = _scrub(out_dict["query"], "$.query")
        return out_dict, changes

    return value, changes
