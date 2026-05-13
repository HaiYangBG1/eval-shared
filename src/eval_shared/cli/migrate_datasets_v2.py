"""
eval-migrate-datasets-v2 — 把旧的单一 `{agent}` Langfuse dataset 迁移到三层架构。

对每个 agent 执行：
  1. 拉旧 `{agent}` dataset 全部 items
  2. **扫 vars 重复**：vars 完全相同的多条 item 自动加 `_variant: 2/3/...` 字段
     避免 `compute_item_id(vars)` 冲突导致后者覆盖前者丢数据（known-issues #3）
  3. 创建 `{agent}-golden`（若不存在）
  4. 把 items 复制到 `{agent}-golden`，item.id 用 `compute_item_id` 复算
     （让后续 sync_dataset push 幂等不重复）
  5. 创建空 `{agent}-regression`、`{agent}-online-temp`
  6. **不删除**旧 `{agent}` dataset——留作只读备份，由人工确认后再删

用法：
  eval-migrate-datasets-v2 --agent intention [--from-name <旧名>] [--dry-run]
  eval-migrate-datasets-v2 --all [--dry-run]

仅一次性运行——迁移完之后 sync_dataset 默认会用 `{agent}-golden`（阶段 6b）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.config import init_env
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.langfuse_client import LangfuseClient


def _discover_agents() -> list[str]:
    """从本地 agents/ 目录推断 agent 名（同 sync_dataset）。"""
    agents_dir = Path.cwd() / "agents"
    if not agents_dir.exists():
        return []
    return sorted(
        name.name
        for name in agents_dir.iterdir()
        if name.is_dir() and not name.name.startswith(".")
    )


def _assign_variant_for_duplicates(
    old_items: list[dict],
) -> list[tuple[dict, object, bool]]:
    """扫源 items，对 vars 完全相同的 item 自动加 `_variant` 字段去重。

    返回 [(old_item, new_vars, variant_assigned), ...] —— 第一次出现的 vars 保持原样
    （variant_assigned=False），第 2/3/... 次出现的加 `_variant: 2/3/...`。

    避免 known-issues #3：相同 vars 在 `compute_item_id` 下会冲突合并丢数据。
    `_variant` 字段以 `_` 开头，PromptFoo / Langfuse 约定不会被 prompt 模板引用，
    所以加这个字段对评估行为零影响。

    input 不是 dict 的 item 原样透传（由调用方跳过处理）。
    """
    import json
    vars_total: dict[str, int] = {}
    for it in old_items:
        v = it.get("input")
        if not isinstance(v, dict):
            continue
        key = json.dumps(v, sort_keys=True, ensure_ascii=False)
        vars_total[key] = vars_total.get(key, 0) + 1

    seen: dict[str, int] = {}
    result: list[tuple[dict, object, bool]] = []
    for it in old_items:
        v = it.get("input")
        if not isinstance(v, dict):
            result.append((it, v, False))
            continue
        key = json.dumps(v, sort_keys=True, ensure_ascii=False)
        seen[key] = seen.get(key, 0) + 1
        if vars_total[key] > 1 and seen[key] > 1:
            new_vars = {**v, "_variant": seen[key]}
            result.append((it, new_vars, True))
        else:
            result.append((it, v, False))
    return result


def _migrate_one(
    client: LangfuseClient,
    agent: str,
    from_name: str,
    dry_run: bool,
    summary: dict,
) -> None:
    golden = f"{agent}-golden"
    regression = f"{agent}-regression"
    online_temp = f"{agent}-online-temp"

    click.echo(f"\n━━━ {agent}: {from_name} → {{golden, regression, online-temp}} ━━━")

    # 1. 拉旧 dataset items
    if client.dataset_exists(from_name):
        old_items = client.get_dataset_items(from_name)
        click.echo(f"  📥 源 `{from_name}` 共 {len(old_items)} items")
    else:
        old_items = []
        click.echo(f"  ⚠️  源 `{from_name}` 不存在，跳过 items 复制（仍会建三个空 dataset）")

    # 自动给重复 vars 加 `_variant` 字段去重（避免 new_id 冲突合并丢数据）
    annotated = _assign_variant_for_duplicates(old_items)
    auto_variant_count = sum(1 for (_it, _v, assigned) in annotated if assigned)
    if auto_variant_count > 0:
        click.echo(
            f"  🔧 发现 {auto_variant_count} 条 vars 重复，自动加 `_variant` 字段去重"
        )

    migrated_at = datetime.now(timezone.utc).isoformat()

    # 2. 建 golden + 复制 items
    if not dry_run and not client.dataset_exists(golden):
        client.create_dataset(golden, f"Migrated from {from_name} on {migrated_at}")
        click.echo(f"  ✨ 新建 {golden}")
        summary["datasets_created"] += 1
    elif client.dataset_exists(golden):
        click.echo(f"  ✓ {golden} 已存在，将合并 items")

    copied = skipped = 0
    for old_item, new_vars, variant_assigned in annotated:
        if not isinstance(new_vars, dict):
            click.echo(
                f"  ⚠️ item {old_item.get('id')!r} 的 input 不是 dict，跳过（无法复算新 id）"
            )
            skipped += 1
            continue
        new_id = compute_item_id(golden, new_vars)
        item_metadata = {
            **(old_item.get("metadata") or {}),
            "migrated_from": from_name,
            "migrated_from_item_id": old_item.get("id"),
            "migrated_at": migrated_at,
        }
        if variant_assigned:
            item_metadata["variant_auto_assigned"] = True
            item_metadata["variant_original_input"] = old_item.get("input")
        body = {
            "datasetName": golden,
            "id": new_id,
            "input": new_vars,
            "expectedOutput": old_item.get("expectedOutput"),
            "metadata": item_metadata,
        }
        if dry_run:
            tag = " [+_variant]" if variant_assigned else ""
            click.echo(
                f"  DRY ✓ {old_item.get('id')} → {golden} (new_id={new_id}){tag}"
            )
            copied += 1
            continue
        try:
            client.upsert_dataset_item(body)
            copied += 1
        except Exception as e:
            click.echo(f"  ❌ {old_item.get('id')!r} 复制失败: {e}", err=True)
            summary["errors"] += 1

    click.echo(f"  ✅ {golden} 复制完成（{copied}/{len(old_items)}，跳过 {skipped}）")
    summary["items_copied"] += copied
    summary["variants_auto_assigned"] = (
        summary.get("variants_auto_assigned", 0) + auto_variant_count
    )

    # 3. 建空 regression / online-temp
    for name in (regression, online_temp):
        if dry_run:
            click.echo(f"  DRY ✓ would ensure {name}")
            continue
        if not client.dataset_exists(name):
            client.create_dataset(
                name, f"Auto-created during v2 migration on {migrated_at}"
            )
            click.echo(f"  ✨ 新建 {name}")
            summary["datasets_created"] += 1
        else:
            click.echo(f"  ✓ {name} 已存在")


@click.command()
@click.option("--agent", help="单个 agent 迁移；与 --all 互斥")
@click.option("--all", "all_agents", is_flag=True, help="扫 agents/ 目录下的所有 agent")
@click.option(
    "--from-name",
    default=None,
    help="源 dataset 名（默认与 agent 同名，例如 `intention`）",
)
@click.option("--dry-run", is_flag=True, help="只打印将要做的操作，不写入 Langfuse")
def main(agent: str | None, all_agents: bool, from_name: str | None, dry_run: bool):
    """把旧 `{agent}` dataset 迁移到三层架构 (`-golden / -regression / -online-temp`)。"""
    init_env()

    if all_agents and agent:
        raise click.ClickException("--agent 与 --all 互斥")
    if not all_agents and not agent:
        raise click.ClickException("必须指定 --agent 或 --all")
    if all_agents and from_name:
        raise click.ClickException(
            "--all 模式下 --from-name 没有意义（每个 agent 用各自同名 dataset 作为源）"
        )

    if all_agents:
        agents = _discover_agents()
        if not agents:
            raise click.ClickException(
                "未在 agents/ 目录下发现任何 agent，无法 --all 模式迁移"
            )
        click.echo(f"📋 将迁移 {len(agents)} 个 agent: {', '.join(agents)}")
    else:
        agents = [agent]  # type: ignore[list-item]

    if dry_run:
        click.echo("⚠️  DRY-RUN 模式，不会实际写入 Langfuse。\n")

    summary = {
        "datasets_created": 0,
        "items_copied": 0,
        "variants_auto_assigned": 0,
        "errors": 0,
    }

    with LangfuseClient() as client:
        for a in agents:
            src = from_name or a
            _migrate_one(client, a, src, dry_run, summary)

    click.echo("")
    click.echo("═══════════════════ 汇总 ═══════════════════")
    click.echo(f"  新建 dataset 数  : {summary['datasets_created']}")
    click.echo(f"  复制 item 数     : {summary['items_copied']}")
    click.echo(f"  错误数           : {summary['errors']}")
    click.echo("")
    click.echo("⚠️  旧 dataset **未删除**——人工确认数据无误后，可在 Langfuse UI 手工删除。")

    if summary["errors"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
