"""轻量测试覆盖 sync_dataset 的命名/路径约定（与三层 dataset 架构对齐）。"""

from __future__ import annotations

from pathlib import Path

from eval_shared.cli.sync_dataset import _default_dataset_name, _local_path


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
