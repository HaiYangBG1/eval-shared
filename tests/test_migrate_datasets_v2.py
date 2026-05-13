from __future__ import annotations

from click.testing import CliRunner

from eval_shared.cli import migrate_datasets_v2
from eval_shared.cli.migrate_datasets_v2 import _assign_variant_for_duplicates
from eval_shared.common.dataset_item_id import compute_item_id


class FakeClient:
    def __init__(
        self,
        items_by_dataset: dict[str, list[dict]] | None = None,
        existing_datasets: set[str] | None = None,
    ) -> None:
        self.items_by_dataset = items_by_dataset or {}
        self.existing_datasets = existing_datasets or set()
        self.created_datasets: list[str] = []
        self.upserted_items: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def dataset_exists(self, name: str) -> bool:
        return name in self.existing_datasets

    def create_dataset(self, name: str, description: str = "") -> dict:
        self.created_datasets.append(name)
        self.existing_datasets.add(name)
        return {"id": "ds-x", "name": name}

    def get_dataset_items(self, name: str) -> list[dict]:
        return list(self.items_by_dataset.get(name, []))

    def upsert_dataset_item(self, body: dict) -> dict:
        self.upserted_items.append(body)
        return {"id": body.get("id") or "auto-id"}


def _invoke(monkeypatch, fake: FakeClient, *args: str):
    monkeypatch.setattr(migrate_datasets_v2, "init_env", lambda: None)
    monkeypatch.setattr(migrate_datasets_v2, "LangfuseClient", lambda: fake)
    return CliRunner().invoke(migrate_datasets_v2.main, list(args))


def test_migrate_one_agent_creates_three_datasets_and_copies_items(monkeypatch) -> None:
    fake = FakeClient(
        items_by_dataset={
            "intention": [
                {"id": "old-1", "input": {"q": "a"}, "expectedOutput": "ok-a", "metadata": {"k": 1}},
                {"id": "old-2", "input": {"q": "b"}, "expectedOutput": "ok-b", "metadata": {}},
            ],
        },
        existing_datasets={"intention"},
    )

    result = _invoke(monkeypatch, fake, "--agent", "intention")

    assert result.exit_code == 0, result.output
    # 三个新 dataset 都建了
    assert "intention-golden" in fake.created_datasets
    assert "intention-regression" in fake.created_datasets
    assert "intention-online-temp" in fake.created_datasets

    # 2 个 item 都复制到 golden，id 用新 dataset_name 复算
    assert len(fake.upserted_items) == 2
    expected_id_a = compute_item_id("intention-golden", {"q": "a"})
    expected_id_b = compute_item_id("intention-golden", {"q": "b"})
    new_ids = {it["id"] for it in fake.upserted_items}
    assert new_ids == {expected_id_a, expected_id_b}

    # metadata 含 migrated_* 审计字段
    for it in fake.upserted_items:
        assert it["metadata"]["migrated_from"] == "intention"
        assert "migrated_at" in it["metadata"]
        assert "migrated_from_item_id" in it["metadata"]


def test_migrate_preserves_existing_target_dataset(monkeypatch) -> None:
    """目标 dataset 已存在时不重建，但仍合并 items（idempotent rerun 安全）。"""
    fake = FakeClient(
        items_by_dataset={"intention": [{"id": "x", "input": {"q": "a"}}]},
        existing_datasets={"intention", "intention-golden"},
    )

    result = _invoke(monkeypatch, fake, "--agent", "intention")

    assert result.exit_code == 0, result.output
    assert "intention-golden" not in fake.created_datasets
    # 但 regression / online-temp 仍要新建
    assert "intention-regression" in fake.created_datasets
    assert "intention-online-temp" in fake.created_datasets
    # items 仍复制
    assert len(fake.upserted_items) == 1


def test_migrate_when_source_missing_still_creates_three_datasets(monkeypatch) -> None:
    fake = FakeClient(existing_datasets=set())
    result = _invoke(monkeypatch, fake, "--agent", "newagent")

    assert result.exit_code == 0, result.output
    assert fake.upserted_items == []
    assert set(fake.created_datasets) == {
        "newagent-golden",
        "newagent-regression",
        "newagent-online-temp",
    }


def test_migrate_dry_run_does_not_write(monkeypatch) -> None:
    fake = FakeClient(
        items_by_dataset={"intention": [{"id": "x", "input": {"q": "a"}}]},
        existing_datasets={"intention"},
    )
    result = _invoke(monkeypatch, fake, "--agent", "intention", "--dry-run")

    assert result.exit_code == 0, result.output
    assert fake.created_datasets == []
    assert fake.upserted_items == []


def test_migrate_skips_items_with_non_dict_input(monkeypatch) -> None:
    """旧 dataset 里偶发的 string/null 输入：跳过不抛。"""
    fake = FakeClient(
        items_by_dataset={
            "intention": [
                {"id": "ok", "input": {"q": "a"}},
                {"id": "bad", "input": "raw-string-input"},
            ]
        },
        existing_datasets={"intention"},
    )
    result = _invoke(monkeypatch, fake, "--agent", "intention")

    assert result.exit_code == 0, result.output
    assert len(fake.upserted_items) == 1
    assert fake.upserted_items[0]["input"] == {"q": "a"}


def test_migrate_from_name_overrides_source(monkeypatch) -> None:
    fake = FakeClient(
        items_by_dataset={"legacy-intent": [{"id": "x", "input": {"q": "a"}}]},
        existing_datasets={"legacy-intent"},
    )
    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--from-name", "legacy-intent",
    )

    assert result.exit_code == 0, result.output
    assert len(fake.upserted_items) == 1
    # 目标是 intention-golden（与 agent 关联），不是 legacy-golden
    assert fake.upserted_items[0]["datasetName"] == "intention-golden"
    assert fake.upserted_items[0]["metadata"]["migrated_from"] == "legacy-intent"


def test_migrate_requires_agent_or_all(monkeypatch) -> None:
    fake = FakeClient()
    result = _invoke(monkeypatch, fake)
    assert result.exit_code != 0
    assert "--agent" in result.output or "--all" in result.output


def test_migrate_agent_and_all_are_mutually_exclusive(monkeypatch) -> None:
    fake = FakeClient()
    result = _invoke(monkeypatch, fake, "--agent", "intention", "--all")
    assert result.exit_code != 0
    assert "互斥" in result.output or "exclusive" in result.output.lower()


# ── _assign_variant_for_duplicates ──


def test_variant_unique_vars_not_modified() -> None:
    """vars 互不重复时不应加任何 _variant 字段。"""
    items = [
        {"id": "a", "input": {"q": "x"}},
        {"id": "b", "input": {"q": "y"}},
        {"id": "c", "input": {"q": "z"}},
    ]
    result = _assign_variant_for_duplicates(items)
    assert [(r[1], r[2]) for r in result] == [
        ({"q": "x"}, False),
        ({"q": "y"}, False),
        ({"q": "z"}, False),
    ]


def test_variant_first_kept_subsequent_numbered() -> None:
    """重复 vars 的第 1 条保持原样，第 2/3/... 条加 _variant: 2/3/..."""
    items = [
        {"id": "a", "input": {"q": "dup"}},
        {"id": "b", "input": {"q": "dup"}},
        {"id": "c", "input": {"q": "dup"}},
    ]
    result = _assign_variant_for_duplicates(items)
    assert result[0][1] == {"q": "dup"}
    assert result[0][2] is False
    assert result[1][1] == {"q": "dup", "_variant": 2}
    assert result[1][2] is True
    assert result[2][1] == {"q": "dup", "_variant": 3}
    assert result[2][2] is True


def test_variant_new_ids_all_unique() -> None:
    """加 _variant 后 compute_item_id 应当为每条算出不同 id。"""
    items = [
        {"id": f"old-{i}", "input": {"q": "same"}} for i in range(5)
    ]
    result = _assign_variant_for_duplicates(items)
    new_ids = {compute_item_id("ds-golden", v) for (_it, v, _a) in result}
    assert len(new_ids) == 5


def test_variant_independent_dup_groups_dont_cross_pollute() -> None:
    """两组各自重复的 vars 独立编号，互不干扰。"""
    items = [
        {"id": "a1", "input": {"q": "A"}},
        {"id": "b1", "input": {"q": "B"}},
        {"id": "a2", "input": {"q": "A"}},
        {"id": "b2", "input": {"q": "B"}},
    ]
    result = _assign_variant_for_duplicates(items)
    # a1/b1 都是首次出现，不加 variant
    assert result[0][1] == {"q": "A"} and result[0][2] is False
    assert result[1][1] == {"q": "B"} and result[1][2] is False
    # a2 是 A 组的第 2 次出现，b2 是 B 组的第 2 次出现，都加 _variant: 2
    assert result[2][1] == {"q": "A", "_variant": 2} and result[2][2] is True
    assert result[3][1] == {"q": "B", "_variant": 2} and result[3][2] is True


def test_variant_non_dict_input_passes_through() -> None:
    """input 不是 dict 的 item 原样透传（由调用方跳过处理），不被编号。"""
    items = [
        {"id": "bad", "input": "raw-string"},
        {"id": "ok", "input": {"q": "a"}},
    ]
    result = _assign_variant_for_duplicates(items)
    assert result[0] == (items[0], "raw-string", False)
    assert result[1] == (items[1], {"q": "a"}, False)


def test_migrate_writes_variant_metadata_for_duplicates(monkeypatch) -> None:
    """重复 vars 触发 _variant 时，item 的 metadata 应记录审计字段。"""
    fake = FakeClient(
        items_by_dataset={
            "intention": [
                {"id": "first", "input": {"q": "dup"}, "metadata": {}},
                {"id": "second", "input": {"q": "dup"}, "metadata": {}},
            ]
        },
        existing_datasets={"intention", "intention-golden"},
    )
    result = _invoke(monkeypatch, fake, "--agent", "intention")
    assert result.exit_code == 0, result.output

    # 第一条原样，第二条加 _variant: 2 + metadata 标记
    assert fake.upserted_items[0]["input"] == {"q": "dup"}
    assert fake.upserted_items[0]["metadata"].get("variant_auto_assigned") is None

    assert fake.upserted_items[1]["input"] == {"q": "dup", "_variant": 2}
    assert fake.upserted_items[1]["metadata"]["variant_auto_assigned"] is True
    assert fake.upserted_items[1]["metadata"]["variant_original_input"] == {"q": "dup"}

    # 两条新 id 不冲突
    assert fake.upserted_items[0]["id"] != fake.upserted_items[1]["id"]
