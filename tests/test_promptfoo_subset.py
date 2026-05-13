from __future__ import annotations

from pathlib import Path

import yaml

from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.promptfoo_subset import (
    cleanup_subset_eval_files,
    filter_dataset_to_miss_subset,
    write_subset_eval_files,
)


# ── compute_item_id（共享算法 sanity check）──


def test_item_id_is_deterministic() -> None:
    """sync_dataset push 和 promptfoo_ab cache 必须算出同样的 id。"""
    a = compute_item_id("intention-golden", {"query": "我一人吃"})
    b = compute_item_id("intention-golden", {"query": "我一人吃"})
    assert a == b
    assert a.startswith("intention-golden-")


def test_item_id_is_independent_of_dict_key_order() -> None:
    a = compute_item_id("ds", {"x": 1, "y": 2})
    b = compute_item_id("ds", {"y": 2, "x": 1})
    assert a == b


def test_item_id_changes_on_input_change() -> None:
    a = compute_item_id("ds", {"q": "A"})
    b = compute_item_id("ds", {"q": "B"})
    assert a != b


# ── filter_dataset_to_miss_subset ──


def test_filter_dataset_keeps_only_miss_cases() -> None:
    full = [
        {"vars": {"q": "a"}, "assert": []},
        {"vars": {"q": "b"}, "assert": []},
        {"vars": {"q": "c"}, "assert": []},
    ]
    id_b = compute_item_id("ds", {"q": "b"})
    id_c = compute_item_id("ds", {"q": "c"})

    subset = filter_dataset_to_miss_subset(
        full,
        dataset_name="ds",
        miss_item_ids={id_b, id_c},
    )

    assert len(subset) == 2
    assert {c["vars"]["q"] for c in subset} == {"b", "c"}


def test_filter_dataset_returns_empty_when_no_miss() -> None:
    full = [{"vars": {"q": "a"}}]
    subset = filter_dataset_to_miss_subset(full, dataset_name="ds", miss_item_ids=set())
    assert subset == []


def test_filter_dataset_skips_non_dict_entries() -> None:
    """YAML 里如果有错误格式的条目（如 null / 字符串）应静默跳过，不抛。"""
    full = [
        {"vars": {"q": "a"}},
        None,
        "garbage",
        {"vars": {"q": "b"}},
    ]
    id_a = compute_item_id("ds", {"q": "a"})
    subset = filter_dataset_to_miss_subset(full, dataset_name="ds", miss_item_ids={id_a})
    assert len(subset) == 1
    assert subset[0]["vars"]["q"] == "a"


# ── write_subset_eval_files / cleanup ──


def test_write_subset_creates_config_and_dataset_in_agent_dir(tmp_path: Path) -> None:
    """临时 config 必须放在 agent 目录（保留相对路径上下文），加 . 前缀。"""
    agent_dir = tmp_path / "agents" / "intention"
    agent_dir.mkdir(parents=True)

    business_config_path = agent_dir / "promptfooconfig.yaml"
    business_config_path.write_text(yaml.safe_dump({
        "prompts": ["file://prompt.yaml"],
        "providers": [{"id": "openai:chat:gpt-4o"}],
        "tests": "file://datasets/golden.yaml",
        "defaultTest": {"options": {"provider": {"id": "x"}}},
    }))

    subset = [{"vars": {"q": "miss-1"}}, {"vars": {"q": "miss-2"}}]

    tmp_config, tmp_dataset = write_subset_eval_files(
        agent="intention",
        dataset_subset=subset,
        business_config_path=business_config_path,
        tmp_basename="promptfoo-ab-baseline-miss",
    )

    assert tmp_config.parent == agent_dir
    assert tmp_dataset.parent == agent_dir
    assert tmp_config.name.startswith(".")
    assert tmp_dataset.name.startswith(".")

    # config 应继承业务字段，只改 tests
    written = yaml.safe_load(tmp_config.read_text())
    assert written["prompts"] == ["file://prompt.yaml"]
    assert written["providers"] == [{"id": "openai:chat:gpt-4o"}]
    assert written["defaultTest"] == {"options": {"provider": {"id": "x"}}}
    # tests 引用的是临时 dataset 同目录文件
    assert written["tests"] == f"file://{tmp_dataset.name}"

    # dataset 应是子集
    written_dataset = yaml.safe_load(tmp_dataset.read_text())
    assert written_dataset == subset


def test_cleanup_removes_files_silently(tmp_path: Path) -> None:
    f1 = tmp_path / ".tmp1.yaml"
    f2 = tmp_path / ".tmp2.yaml"
    f1.write_text("x: 1")
    f2.write_text("y: 2")

    cleanup_subset_eval_files(f1, f2)

    assert not f1.exists()
    assert not f2.exists()


def test_cleanup_tolerates_nonexistent_files(tmp_path: Path) -> None:
    """部分清理失败不应抛——临时文件可能已被并发删除或没生成。"""
    cleanup_subset_eval_files(tmp_path / "never_existed.yaml")
    # 没异常即通过
