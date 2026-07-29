from __future__ import annotations

from click.testing import CliRunner

from eval_shared.cli import dataset_promote
from eval_shared.cli.dataset_promote import (
    _source_dataset_name,
    _target_dataset_name,
)
from eval_shared.common.dataset_item_id import compute_item_id


# ── 命名约定 ──


def test_source_defaults_to_online_temp() -> None:
    assert _source_dataset_name("intention", None) == "intention-online-temp"


def test_source_can_be_overridden() -> None:
    assert _source_dataset_name("intention", "custom-source") == "custom-source"


def test_target_kind_maps_to_dataset_suffix() -> None:
    assert _target_dataset_name("intention", "golden") == "intention-golden"
    assert _target_dataset_name("recommend", "regression") == "recommend-regression"


# ── CLI 行为 ──


class FakeClient:
    """收集 promote 调用的轻量 mock。"""

    def __init__(
        self,
        items_by_id: dict[str, dict] | None = None,
        existing_datasets: set[str] | None = None,
        list_items_payload: list[dict] | None = None,
    ) -> None:
        self.items_by_id = items_by_id or {}
        self.existing_datasets = existing_datasets or set()
        self.list_items_payload = list_items_payload or []
        self.created_datasets: list[str] = []
        self.upserted_items: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_dataset_item(self, item_id: str) -> dict:
        if item_id not in self.items_by_id:
            raise RuntimeError(f"404 item {item_id}")
        return self.items_by_id[item_id]

    def dataset_exists(self, name: str) -> bool:
        return name in self.existing_datasets

    def create_dataset(self, name: str, description: str = "") -> dict:
        self.created_datasets.append(name)
        self.existing_datasets.add(name)
        return {"id": "ds-new", "name": name}

    def upsert_dataset_item(self, body: dict) -> dict:
        self.upserted_items.append(body)
        return {"id": body.get("id") or "auto-id"}

    def get_dataset_items(self, _name: str) -> list[dict]:
        return self.list_items_payload


def _invoke(monkeypatch, fake: FakeClient, *args: str):
    monkeypatch.setattr(dataset_promote, "init_env", lambda: None)
    monkeypatch.setattr(dataset_promote, "LangfuseClient", lambda: fake)
    return CliRunner().invoke(dataset_promote.main, list(args))


def test_promote_copies_item_with_promoted_metadata(monkeypatch) -> None:
    src_input = {"query": "我对花生过敏"}
    fake = FakeClient(
        items_by_id={
            "src-1": {
                "id": "src-1",
                "input": src_input,
                "expectedOutput": None,
                "metadata": {"source": "eval-online", "score_value": 0.0},
            }
        },
        existing_datasets={"intention-online-temp", "intention-regression"},
    )

    # --no-local-write：本测试只核 Langfuse 侧行为（本地双写见下方 §2.3 测试组）
    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--reason", "新发现的过敏咨询 edge case",
        "--no-local-write",
    )

    assert result.exit_code == 0, result.output
    assert len(fake.upserted_items) == 1
    body = fake.upserted_items[0]
    assert body["datasetName"] == "intention-regression"
    assert body["input"] == src_input
    # 目标 item.id 用共享算法复算（保证幂等）
    assert body["id"] == compute_item_id("intention-regression", src_input)
    # metadata 含原始字段 + promote 审计字段
    meta = body["metadata"]
    assert meta["source"] == "eval-online"
    assert meta["promoted_from"] == "intention-online-temp"
    assert meta["promoted_from_item_id"] == "src-1"
    assert meta["promoted_reason"] == "新发现的过敏咨询 edge case"
    assert "promoted_at" in meta


def test_promote_list_input_parsed_to_dict_vars_with_stable_id(
    monkeypatch, tmp_path
) -> None:
    """#39 方案 A：messages 数组按映射解析成 dict 型 vars，id 基于解析后 vars。

    #17 的幂等根基保持：同一输入重复 promote 得到同一 id，不产生重复 item。
    """
    _chdir_repo_root(monkeypatch, tmp_path)
    src_input = [
        {"role": "system", "content": "你是点餐助手"},
        {"role": "user", "content": "来一份小炒肉"},
    ]
    fake = FakeClient(
        items_by_id={
            "src-1": {"id": "src-1", "input": src_input, "metadata": {}}
        },
        existing_datasets={"intention-online-temp", "intention-regression"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--no-local-write",
    )

    assert result.exit_code == 0, result.output
    body = fake.upserted_items[0]
    parsed = {"query": "来一份小炒肉"}
    assert body["input"] == parsed
    assert body["id"] == compute_item_id("intention-regression", parsed)
    # 同一输入重算必须得到同一 id（幂等的根基）
    assert body["id"] == compute_item_id("intention-regression", dict(parsed))


def test_promote_creates_target_dataset_if_missing(monkeypatch) -> None:
    fake = FakeClient(
        items_by_id={
            "src-1": {"id": "src-1", "input": {"q": "a"}, "metadata": {}}
        },
        existing_datasets={"intention-online-temp"},  # regression 尚未建
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--no-local-write",
    )

    assert result.exit_code == 0, result.output
    assert "intention-regression" in fake.created_datasets


def test_promote_dry_run_does_not_write(monkeypatch) -> None:
    fake = FakeClient(
        items_by_id={"src-1": {"id": "src-1", "input": {"q": "a"}, "metadata": {}}},
        existing_datasets={"intention-online-temp"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
        "--item-ids", "src-1",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert fake.upserted_items == []
    assert fake.created_datasets == []  # dry-run 也不该建 dataset


def test_promote_handles_partial_failure(monkeypatch) -> None:
    fake = FakeClient(
        items_by_id={
            "src-1": {"id": "src-1", "input": {"q": "a"}, "metadata": {}},
            # src-2 缺失 → get_dataset_item 抛
        },
        existing_datasets={"intention-online-temp", "intention-golden"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
        "--item-ids", "src-1,src-2",
    )

    # 部分失败 → exit code 非 0，但成功的那条应该已写入
    assert result.exit_code != 0
    assert len(fake.upserted_items) == 1
    assert fake.upserted_items[0]["input"] == {"q": "a"}


def test_promote_requires_item_ids_unless_list_mode(monkeypatch) -> None:
    fake = FakeClient(existing_datasets={"intention-online-temp"})

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
    )

    assert result.exit_code != 0
    assert "item-ids" in result.output.lower() or "list" in result.output.lower()


def test_promote_list_mode_does_not_promote(monkeypatch) -> None:
    fake = FakeClient(
        existing_datasets={"intention-online-temp"},
        list_items_payload=[
            {"id": "a", "input": {"q": "x"}, "metadata": {"score_value": 1.0}},
            {"id": "b", "input": {"q": "y"}, "metadata": {"score_value": 0.0}},
        ],
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
        "--list",
    )

    assert result.exit_code == 0, result.output
    assert fake.upserted_items == []
    assert "intention-online-temp" in result.output
    assert "共 2 条" in result.output


# ── 本地双写（契约 §2.3：regression 本地 YAML=SSOT，Langfuse 仅镜像）──


from eval_shared.common.yaml_utils import load_yaml  # noqa: E402


_MESSAGES_INPUT = [
    {"role": "system", "content": '菜单 {"food_id":"1300000021","stock":907}'},
    {"role": "user", "content": "订餐电话13812345678"},
]


def _fake_with_messages_item() -> FakeClient:
    return FakeClient(
        items_by_id={
            "src-1": {
                "id": "src-1",
                "input": _MESSAGES_INPUT,
                "metadata": {"source": "eval-online", "score_value": 0.0},
            }
        },
        existing_datasets={"intention-online-temp", "intention-regression"},
    )


def _chdir_repo_root(monkeypatch, tmp_path, agent: str = "intention") -> None:
    datasets = tmp_path / "agents" / agent / "datasets"
    datasets.mkdir(parents=True)
    # 契约 §2.3：regression promote 需 per-agent 变量映射（intention 类无上下文段落）
    (datasets / "var-mapping.yaml").write_text(
        "query_var: query\nsections: {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)


def test_promote_regression_writes_local_ssot_scrubbed(monkeypatch, tmp_path) -> None:
    _chdir_repo_root(monkeypatch, tmp_path)
    fake = _fake_with_messages_item()

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--reason", "生产幻觉实锤",
    )

    assert result.exit_code == 0, result.output
    entries = load_yaml(tmp_path / "agents" / "intention" / "datasets" / "regression.yaml")
    assert len(entries) == 1
    entry = entries[0]
    # #39 方案 A：消息数组解析为 dict 型 vars（promptfoo 可直接消费），system 消息丢弃
    parsed = {"query": "订餐电话13812345678"}
    # id 用未脱敏解析结果复算（与 Langfuse 镜像一致，往返幂等锚点）
    assert entry["id"] == compute_item_id("intention-regression", parsed)
    # 本地入 git 的用户话术已脱敏
    assert entry["vars"] == {"query": "订餐电话<PHONE>"}
    # 审计字段齐全（assert 不入 metadata，有独立位置）
    meta = entry["metadata"]
    assert meta["promoted_from"] == "intention-online-temp"
    assert meta["promoted_reason"] == "生产幻觉实锤"
    assert "promoted_at" in meta and "assert" not in meta
    assert entry["assert"] == []
    # Langfuse 镜像保持原文（Judge 链路不脱敏，2026-07-28 拍板）
    assert fake.upserted_items[0]["input"] == parsed
    assert "PII" in result.output and "<PHONE>" in result.output


def test_promote_regression_local_merge_is_idempotent(monkeypatch, tmp_path) -> None:
    _chdir_repo_root(monkeypatch, tmp_path)

    for _ in range(2):
        fake = _fake_with_messages_item()
        result = _invoke(
            monkeypatch, fake,
            "--agent", "intention",
            "--to", "regression",
            "--item-ids", "src-1",
        )
        assert result.exit_code == 0, result.output

    entries = load_yaml(tmp_path / "agents" / "intention" / "datasets" / "regression.yaml")
    assert len(entries) == 1  # 同 id 覆盖，不追加重复条目


def test_promote_regression_fails_fast_without_agents_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # 没有 agents/ 目录（如误在 eval-shared 根运行）
    fake = _fake_with_messages_item()

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
    )

    # 早失败：Langfuse 一条都不该写（避免「镜像有、SSOT 没有」的半程状态）
    assert result.exit_code != 0
    assert fake.upserted_items == []
    assert "no-local-write" in result.output


def test_promote_regression_no_local_write_skips_file(monkeypatch, tmp_path) -> None:
    _chdir_repo_root(monkeypatch, tmp_path)
    fake = _fake_with_messages_item()

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--no-local-write",
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "agents" / "intention" / "datasets" / "regression.yaml").exists()
    assert "已关闭本地回写" in result.output
    assert len(fake.upserted_items) == 1


def test_promote_regression_dry_run_previews_local_write(monkeypatch, tmp_path) -> None:
    _chdir_repo_root(monkeypatch, tmp_path)
    fake = _fake_with_messages_item()

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "agents" / "intention" / "datasets" / "regression.yaml").exists()
    assert "DRY 本地回写" in result.output
    assert fake.upserted_items == []


def test_promote_regression_messages_without_mapping_fails_item(
    monkeypatch, tmp_path
) -> None:
    """契约 §2.3：消息数组 + 无 var-mapping.yaml → 该条硬失败，Langfuse/本地都不写。"""
    (tmp_path / "agents" / "intention" / "datasets").mkdir(parents=True)  # 故意不放映射
    monkeypatch.chdir(tmp_path)
    fake = _fake_with_messages_item()

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
    )

    assert result.exit_code != 0
    assert fake.upserted_items == []
    assert "var-mapping.yaml" in result.output


def test_promote_regression_section_missing_fails_item(monkeypatch, tmp_path) -> None:
    """契约 §2.3：配置的段落标题在消息中缺失 → 硬失败，不写半程、不猜测。"""
    datasets = tmp_path / "agents" / "recommend" / "datasets"
    datasets.mkdir(parents=True)
    (datasets / "var-mapping.yaml").write_text(
        'query_var: query\nsections:\n  "Menu Data": menu_data\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    fake = FakeClient(
        items_by_id={
            "src-1": {
                "id": "src-1",
                "input": [
                    {"role": "user", "content": "# Context Information\n\n## 1. [Rule Class]\n\n单人餐"},
                    {"role": "user", "content": "来个招牌菜"},
                ],
                "metadata": {},
            }
        },
        existing_datasets={"recommend-online-temp", "recommend-regression"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "recommend",
        "--to", "regression",
        "--item-ids", "src-1",
    )

    assert result.exit_code != 0
    assert fake.upserted_items == []
    assert "[Menu Data]" in result.output


def test_promote_regression_dict_input_passthrough(monkeypatch, tmp_path) -> None:
    """契约 §2.3 兜底：input 已是 dict → 原样透传（幂等，重复 promote 安全）。"""
    _chdir_repo_root(monkeypatch, tmp_path)
    src_input = {"query": "来一份小炒肉", "rule_class": "推荐菜"}
    fake = FakeClient(
        items_by_id={"src-1": {"id": "src-1", "input": src_input, "metadata": {}}},
        existing_datasets={"intention-online-temp", "intention-regression"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
    )

    assert result.exit_code == 0, result.output
    assert fake.upserted_items[0]["input"] == src_input
    assert fake.upserted_items[0]["id"] == compute_item_id(
        "intention-regression", src_input
    )


def test_promote_regression_multi_turn_marked(monkeypatch, tmp_path) -> None:
    """契约 §2.3④：多轮观测历史轮丢弃，但必须打 multi_turn 标记 + 喊出来。"""
    _chdir_repo_root(monkeypatch, tmp_path)
    src_input = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "推荐当季新品"},
        {"role": "assistant", "content": "好的，为您推荐…"},
        {"role": "user", "content": "这不好吃"},
    ]
    fake = FakeClient(
        items_by_id={"src-1": {"id": "src-1", "input": src_input, "metadata": {}}},
        existing_datasets={"intention-online-temp", "intention-regression"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
    )

    assert result.exit_code == 0, result.output
    assert "多轮观测" in result.output
    assert fake.upserted_items[0]["input"] == {"query": "这不好吃"}
    assert fake.upserted_items[0]["metadata"]["multi_turn"] is True
    entries = load_yaml(tmp_path / "agents" / "intention" / "datasets" / "regression.yaml")
    assert entries[0]["metadata"]["multi_turn"] is True


def test_promote_golden_keeps_raw_input(monkeypatch, tmp_path) -> None:
    """#39 范围纪律：--to golden 不做变量解析，保持原行为（断言需人工设计）。"""
    _chdir_repo_root(monkeypatch, tmp_path)
    src_input = [
        {"role": "system", "content": "你是点餐助手"},
        {"role": "user", "content": "来一份小炒肉"},
    ]
    fake = FakeClient(
        items_by_id={"src-1": {"id": "src-1", "input": src_input, "metadata": {}}},
        existing_datasets={"intention-online-temp", "intention-golden"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
        "--item-ids", "src-1",
    )

    assert result.exit_code == 0, result.output
    assert fake.upserted_items[0]["input"] == src_input
    assert fake.upserted_items[0]["id"] == compute_item_id(
        "intention-golden", src_input
    )


def test_promote_golden_reminds_manual_ssot(monkeypatch, tmp_path) -> None:
    _chdir_repo_root(monkeypatch, tmp_path)
    fake = FakeClient(
        items_by_id={"src-1": {"id": "src-1", "input": {"q": "a"}, "metadata": {}}},
        existing_datasets={"intention-online-temp", "intention-golden"},
    )

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "golden",
        "--item-ids", "src-1",
    )

    assert result.exit_code == 0, result.output
    assert "不自动回写" in result.output
