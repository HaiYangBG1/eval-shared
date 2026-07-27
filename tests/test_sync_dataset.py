"""轻量测试覆盖 sync_dataset 的命名/路径约定（与三层 dataset 架构对齐）
+ regression 无损往返（契约 §2.3：id/审计 metadata 保留、入 git 前脱敏）。"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from eval_shared.cli import sync_dataset
from eval_shared.cli.sync_dataset import _default_dataset_name, _local_path
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.yaml_utils import dump_yaml, load_yaml


def test_default_dataset_name_for_golden() -> None:
    assert _default_dataset_name("intention", "golden") == "intention-golden"


def test_default_dataset_name_for_regression() -> None:
    assert _default_dataset_name("recommend", "regression") == "recommend-regression"


def test_default_dataset_name_for_online_temp() -> None:
    assert _default_dataset_name("replenish", "online-temp") == "replenish-online-temp"


def test_local_path_uses_type_as_filename(tmp_path, monkeypatch) -> None:
    """本地 YAML 路径 = agents/{agent}/datasets/{type}.yaml。"""
    monkeypatch.chdir(tmp_path)

    p_golden = _local_path("intention", "golden")
    p_regression = _local_path("intention", "regression")
    p_online = _local_path("intention", "online-temp")

    assert p_golden == tmp_path / "agents" / "intention" / "datasets" / "golden.yaml"
    assert p_regression == tmp_path / "agents" / "intention" / "datasets" / "regression.yaml"
    assert p_online == tmp_path / "agents" / "intention" / "datasets" / "online-temp.yaml"


# ── regression 无损往返（契约 §2.3）──


class FakeClient:
    def __init__(self, items: list[dict] | None = None, existing: set[str] | None = None):
        self.items = items or []
        self.existing = existing or set()
        self.upserted: list[dict] = []
        self.created: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_dataset(self, name: str) -> dict:
        return {"name": name}

    def get_dataset_items(self, _name: str) -> list[dict]:
        return self.items

    def dataset_exists(self, name: str) -> bool:
        return name in self.existing

    def create_dataset(self, name: str, description: str = "") -> dict:
        self.created.append(name)
        self.existing.add(name)
        return {"name": name}

    def upsert_dataset_item(self, body: dict) -> dict:
        self.upserted.append(body)
        return {"id": body.get("id")}


def _invoke(monkeypatch, fake: FakeClient, *args: str):
    monkeypatch.setattr(sync_dataset, "init_env", lambda: None)
    monkeypatch.setattr(sync_dataset, "LangfuseClient", lambda: fake)
    return CliRunner().invoke(sync_dataset.main, list(args))


def test_pull_regression_preserves_id_metadata_and_scrubs(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    items = [
        {
            "id": "recommend-regression-bbb",
            "input": [
                {"role": "system", "content": '菜单 {"food_id":"1300000021"}'},
                {"role": "user", "content": "张先生订餐 13812345678"},
            ],
            "metadata": {
                "promoted_from": "recommend-online-temp",
                "promoted_at": "2026-07-27T10:00:00+00:00",
                "promoted_reason": "菜品幻觉",
                "assert": [],
                "index": 3,
            },
        },
        {
            "id": "recommend-regression-aaa",
            "input": {"query": "会员号 8888888 点餐"},
            "metadata": {"promoted_at": "2026-07-26T09:00:00+00:00"},
        },
    ]
    fake = FakeClient(items=items)

    result = _invoke(
        monkeypatch, fake,
        "--agent", "recommend", "--type", "regression", "--direction", "pull",
    )

    assert result.exit_code == 0, result.output
    assert "SSOT" in result.output  # pull 覆盖警告
    entries = load_yaml(tmp_path / "agents" / "recommend" / "datasets" / "regression.yaml")
    assert len(entries) == 2
    # 按 promoted_at 排序（文件确定性）
    assert entries[0]["id"] == "recommend-regression-aaa"
    assert entries[1]["id"] == "recommend-regression-bbb"
    # 审计 metadata 保留；assert/index 不重复入 metadata
    meta = entries[1]["metadata"]
    assert meta["promoted_reason"] == "菜品幻觉"
    assert "assert" not in meta and "index" not in meta
    # 入 git 前脱敏：user 话术脱、system 菜单不碰
    assert entries[1]["vars"][1]["content"] == "<NAME>订餐 <PHONE>"
    assert "1300000021" in entries[1]["vars"][0]["content"]
    assert entries[0]["vars"]["query"] == "会员号<ID> 点餐"


def test_push_regression_uses_stored_id_and_returns_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "agents" / "recommend" / "datasets" / "regression.yaml"
    dump_yaml(
        [{
            "id": "recommend-regression-fixed1",
            "vars": [{"role": "user", "content": "订餐电话<PHONE>"}],
            "assert": [],
            "metadata": {"promoted_reason": "菜品幻觉", "promoted_at": "2026-07-27T10:00:00+00:00"},
        }],
        local,
    )
    fake = FakeClient(existing={"recommend-regression"})

    result = _invoke(
        monkeypatch, fake,
        "--agent", "recommend", "--type", "regression", "--direction", "push",
    )

    assert result.exit_code == 0, result.output
    body = fake.upserted[0]
    # 脱敏后 hash 会漂移 → 必须用存量 id 幂等覆盖，不重算
    assert body["id"] == "recommend-regression-fixed1"
    assert body["metadata"]["promoted_reason"] == "菜品幻觉"
    assert body["metadata"]["assert"] == [] and body["metadata"]["index"] == 0


def test_push_entry_without_id_falls_back_to_computed(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "agents" / "intention" / "datasets" / "golden.yaml"
    vars_data = {"query": "来个招牌菜"}
    dump_yaml([{"vars": vars_data, "assert": []}], local)
    fake = FakeClient(existing={"intention-golden"})

    result = _invoke(
        monkeypatch, fake,
        "--agent", "intention", "--type", "golden", "--direction", "push",
    )

    assert result.exit_code == 0, result.output
    assert fake.upserted[0]["id"] == compute_item_id("intention-golden", vars_data)
