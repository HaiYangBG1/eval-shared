"""
eval-sync-dataset — Langfuse Dataset ↔ 本地 YAML 双向同步。

三层 dataset 架构（与 eval-migrate-datasets-v2 / eval-dataset-promote 对齐）：
  - golden       → 当前能力基线，A/B 主战场
  - regression   → 历史 bug 沉淀，CI 必跑
  - online-temp  → eval-online 工作区（覆盖式，一般不需要 push）

默认 dataset 名约定 `{agent}-{type}`，本地路径 `agents/{agent}/datasets/{type}.yaml`。

用法：
  eval-sync-dataset --agent <name> [--type golden|regression|online-temp] [--direction pull|push]
  eval-sync-dataset --all           [--type ...] [--direction ...]
  eval-sync-dataset --agent <name> --dataset <custom-name>   # 显式指定 dataset 名
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx

from eval_shared.common.config import init_env, get_langfuse_config
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.yaml_utils import load_yaml, dump_yaml


def _discover_agents() -> list[str]:
    agents_dir = Path.cwd() / "agents"
    if not agents_dir.exists():
        return []
    return sorted(
        name.name
        for name in agents_dir.iterdir()
        if name.is_dir() and (name / "datasets" / "golden.yaml").exists()
    )


def _derive_expected_output(assert_arr: list[dict]) -> dict | None:
    """
    从 PromptFoo 断言数组中提取 expectedOutput。
    仅适用于「确定性输出」的 Agent（如意图识别）。
    """
    if not assert_arr:
        return None

    result = {}
    for a in assert_arr:
        if a.get("type") != "javascript" or not isinstance(a.get("value"), str):
            continue
        code = a["value"]
        # .intent === 'xxx'
        m = re.search(r"\.intent\s*===\s*['\"]([^'\"]+)['\"]", code)
        if m:
            result["intent"] = m.group(1)
        # .rule.includes('xxx')
        m = re.search(r"\.rule\.includes\(['\"]([^'\"]+)['\"]\)", code)
        if m:
            result["rule"] = f"含「{m.group(1)}」"

    return result if result.get("intent") else None


_VALID_TYPES = ("golden", "regression", "online-temp")


def _local_path(agent: str, type_: str) -> Path:
    """本地 dataset YAML 路径。type 决定文件名（golden.yaml / regression.yaml / online-temp.yaml）。"""
    return Path.cwd() / "agents" / agent / "datasets" / f"{type_}.yaml"


def _default_dataset_name(agent: str, type_: str) -> str:
    """默认 Langfuse dataset 名约定：`{agent}-{type}`。"""
    return f"{agent}-{type_}"


def _pull(client: LangfuseClient, agent: str, dataset_name: str, type_: str) -> None:
    click.echo(f'📥 [{agent}] GET dataset "{dataset_name}"')

    # 确认 dataset 存在
    try:
        client.get_dataset(dataset_name)
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"获取 dataset 失败：{e.response.status_code} {e.response.reason_phrase}"
        )

    items = client.get_dataset_items(dataset_name)

    # 还原为 PromptFoo 测试格式
    items_sorted = sorted(items, key=lambda x: (x.get("metadata") or {}).get("index", 0))
    tests = []
    for item in items_sorted:
        metadata = item.get("metadata") or {}
        assert_arr = metadata.get("assert", [])
        if not isinstance(assert_arr, list):
            assert_arr = []

        if not assert_arr and item.get("expectedOutput"):
            assert_arr = [{
                "type": "llm-rubric",
                "value": f"期望输出应接近：{json.dumps(item['expectedOutput'], ensure_ascii=False)}",
            }]

        entry: dict = {"vars": item.get("input", {}), "assert": assert_arr}
        if item.get("expectedOutput"):
            entry["expectedOutput"] = item["expectedOutput"]
        tests.append(entry)

    out_path = _local_path(agent, type_)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = "\n".join([
        "# 由 eval-sync-dataset 从 Langfuse 同步生成，请勿手动修改。",
        f'# 来源    : dataset="{dataset_name}"',
        f"# 条目数  : {len(tests)}",
        f"# 同步时间: {datetime.now(timezone.utc).isoformat()}",
        "",
        "",
    ])

    dump_yaml(tests, out_path, header=header)
    rel = out_path.relative_to(Path.cwd())
    click.echo(f"   ✅ {len(tests)} 条 → {rel}")


def _push(client: LangfuseClient, agent: str, dataset_name: str, type_: str) -> None:
    local_path = _local_path(agent, type_)
    if not local_path.exists():
        raise click.ClickException(f"文件不存在：{local_path}")

    if type_ == "online-temp":
        click.echo(
            "   ⚠️  push 到 online-temp 通常不需要——它是 eval-online 工作区，"
            "下次 eval-online 跑会被清空覆盖。"
        )

    tests = load_yaml(local_path)
    if not isinstance(tests, list):
        raise click.ClickException(f"{local_path.name} 必须是数组 [{{ vars, assert }}, ...]")

    click.echo(f'📤 [{agent}] 准备推送 {len(tests)} 条到 dataset "{dataset_name}"')

    # 确保 dataset 存在
    if not client.dataset_exists(dataset_name):
        client.create_dataset(
            dataset_name,
            f"Synced from eval-ai-order/{agent} on {datetime.now(timezone.utc).isoformat()}",
        )
        click.echo(f'   ✨ 新建 dataset "{dataset_name}"')
    else:
        click.echo("   ✓ dataset 已存在")

    ok = fail = 0
    for i, t in enumerate(tests):
        if not isinstance(t, dict):
            t = {}
        input_data = t.get("vars", {})
        assert_arr = t.get("assert", []) if isinstance(t.get("assert"), list) else []

        item_id = compute_item_id(dataset_name, input_data)

        expected = t.get("expectedOutput") or _derive_expected_output(assert_arr)

        body = {
            "datasetName": dataset_name,
            "id": item_id,
            "input": input_data,
            "expectedOutput": expected,
            "metadata": {
                "assert": assert_arr,
                "source": f"eval-ai-order/{agent}",
                "index": i,
            },
        }

        try:
            client.upsert_dataset_item(body)
            ok += 1
        except httpx.HTTPStatusError as e:
            fail += 1
            click.echo(
                f"   ⚠️  #{i} {e.response.status_code} {e.response.reason_phrase}",
                err=True,
            )

    click.echo(f"   ✅ 上传完成：成功 {ok}，失败 {fail}（共 {len(tests)}）")
    if fail > 0:
        raise click.ClickException(f"{fail} 条 item 上传失败")


@click.command()
@click.option("--agent", default=None, help="Agent 名称")
@click.option("--all", "sync_all", is_flag=True, help="同步所有 Agent")
@click.option(
    "--direction",
    type=click.Choice(["pull", "push"]),
    default="pull",
    help="同步方向（默认 pull）",
)
@click.option(
    "--type",
    "type_",
    type=click.Choice(_VALID_TYPES, case_sensitive=False),
    default="golden",
    help="数据集类别：golden（默认）/ regression / online-temp。决定 Langfuse dataset 名后缀和本地 YAML 文件名。",
)
@click.option(
    "--dataset",
    default=None,
    help="显式 Langfuse Dataset 名（覆盖 {agent}-{type} 默认）",
)
def main(
    agent: str | None,
    sync_all: bool,
    direction: str,
    type_: str,
    dataset: str | None,
):
    """Langfuse Dataset ↔ 本地 YAML 双向同步。

    默认 Langfuse dataset 名 = {agent}-{type}，本地路径 = agents/{agent}/datasets/{type}.yaml。
    """
    if not agent and not sync_all:
        click.echo(
            "用法：eval-sync-dataset --agent <name> [--type golden|regression|online-temp] [--direction pull|push]\n"
            "      eval-sync-dataset --all [--type ...] [--direction ...]",
            err=True,
        )
        raise SystemExit(1)

    init_env()
    agents = _discover_agents() if sync_all else [agent]
    if not agents:
        raise click.ClickException(
            "未发现任何 agent（需要 agents/<name>/ 目录存在）"
        )

    action = "拉取" if direction == "pull" else "上传"
    click.echo(f"🔄 {action} Dataset (type={type_})")
    click.echo(f"   Agents   : {', '.join(agents)}")
    click.echo("")

    failures = []
    with LangfuseClient() as client:
        for ag in agents:
            ds_name = dataset or _default_dataset_name(ag, type_)
            try:
                if direction == "pull":
                    _pull(client, ag, ds_name, type_)
                else:
                    _push(client, ag, ds_name, type_)
            except (click.ClickException, Exception) as e:
                click.echo(f"❌ [{ag}] {e}", err=True)
                failures.append(ag)
            click.echo("")

    if failures:
        click.echo(f"⚠️  以下 agent 同步失败：{', '.join(failures)}", err=True)
        raise SystemExit(1)
    click.echo("✅ 全部同步完成")


if __name__ == "__main__":
    main()
