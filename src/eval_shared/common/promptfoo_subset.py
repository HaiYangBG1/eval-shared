"""PromptFoo 子集评估辅助：临时 dataset YAML + 临时 promptfooconfig 生成与清理。

用于 A/B 缓存命中场景：只跑 miss 的 case，从而省 LLM 调用成本。

为什么要"临时配置"：业务项目的 promptfooconfig.yaml 里 `tests: file://datasets/golden.yaml`
是写死的，要让 PromptFoo 跑 dataset 子集，最干净的方式是临时生成一份小 dataset 和
对应的 promptfooconfig，用 `-c <临时config>` 跑完即删，不污染业务项目。

为什么临时 config 必须放在 agents/<agent>/ 而不是 output/：
business config 里的相对路径（如 `prompts: file://prompt.yaml`）是相对于 config
所在目录解析的。临时 config 放 output/ 会让所有相对路径都失效。
"""

from __future__ import annotations

from pathlib import Path

from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.yaml_utils import dump_yaml, load_yaml


def filter_dataset_to_miss_subset(
    full_dataset: list,
    *,
    dataset_name: str,
    miss_item_ids: set[str],
) -> list:
    """从全集 dataset YAML 中过滤出 miss 子集。

    Args:
        full_dataset: 业务项目本地 dataset YAML 解析后的列表
        dataset_name: Langfuse dataset 名（用于复算 item_id 与 miss set 比对）
        miss_item_ids: 需要实跑的 item id 集合
    """
    subset: list = []
    for case in full_dataset:
        if not isinstance(case, dict):
            continue
        vars_data = case.get("vars", {})
        item_id = compute_item_id(dataset_name, vars_data)
        if item_id in miss_item_ids:
            subset.append(case)
    return subset


def write_subset_eval_files(
    *,
    agent: str,
    dataset_subset: list,
    business_config_path: Path,
    tmp_basename: str,
) -> tuple[Path, Path]:
    """为 PromptFoo 子集跑生成临时 dataset 和 config 文件。

    临时 config 放在 `agents/<agent>/.{tmp_basename}.config.yaml`，
    临时 dataset 放在 `agents/<agent>/.{tmp_basename}.dataset.yaml`，
    都加 `.` 前缀便于 .gitignore 识别为临时产物。

    Args:
        agent: agent 名（决定临时文件目录）
        dataset_subset: 已经过滤的 dataset 子集
        business_config_path: 业务项目原 promptfooconfig.yaml 路径
        tmp_basename: 临时文件基名（如 "promptfoo-ab-baseline-miss"）

    Returns:
        (tmp_config_path, tmp_dataset_path) — 调用方跑完后传给 cleanup
    """
    agent_dir = business_config_path.parent
    tmp_dataset = agent_dir / f".{tmp_basename}.dataset.yaml"
    tmp_config = agent_dir / f".{tmp_basename}.config.yaml"

    dump_yaml(dataset_subset, tmp_dataset)

    # 继承业务 config，只覆盖 tests 字段；相对路径仍正确解析
    business_config = load_yaml(business_config_path)
    if not isinstance(business_config, dict):
        raise ValueError(
            f"业务 promptfooconfig 不是 dict 结构：{business_config_path}"
        )
    tmp_config_data = {
        **business_config,
        "tests": f"file://{tmp_dataset.name}",  # 同目录下相对引用
    }
    dump_yaml(tmp_config_data, tmp_config)

    return tmp_config, tmp_dataset


def cleanup_subset_eval_files(*paths: Path) -> None:
    """容错删除临时文件——任一删除失败不影响其他。"""
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
