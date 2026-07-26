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

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention",
        "--to", "regression",
        "--item-ids", "src-1",
        "--reason", "新发现的过敏咨询 edge case",
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


def test_promote_list_input_gets_stable_id(monkeypatch) -> None:
    """#17：Dify obs 的 messages 数组（list 型 input）也必须算确定性 id。

    07-23 起三节点 obs input 均为 messages 数组；若 id=None，重复 promote
    时 Langfuse 分配随机 id → 目标数据集产生重复 item。
    """
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
    )

    assert result.exit_code == 0, result.output
    body = fake.upserted_items[0]
    assert body["id"] == compute_item_id("intention-regression", src_input)
    # 同一输入重算必须得到同一 id（幂等的根基）
    assert compute_item_id("intention-regression", src_input) == compute_item_id(
        "intention-regression", list(src_input)
    )


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
