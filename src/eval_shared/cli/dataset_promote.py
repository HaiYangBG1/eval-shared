"""
eval-dataset-promote — 把 online-temp dataset 里指定 item 转入 golden 或 regression。

用法：
  # 列出源 dataset 的 item 帮你挑
  eval-dataset-promote --agent intention --list

  # 把指定 item 转入 regression
  eval-dataset-promote --agent intention --to regression \\
      --item-ids cmou123abc,cmou456def \\
      --reason "新发现的过敏咨询 edge case"

  # 转入 golden（默认 to）
  eval-dataset-promote --agent intention --to golden --item-ids ...

  # dry-run 看会做什么不实际写入
  eval-dataset-promote --agent intention --to golden --item-ids ... --dry-run

设计：
  - 源 dataset 默认 `{agent}-online-temp`，可用 --from 覆盖
  - 目标 dataset 由 --to 决定（golden / regression）→ `{agent}-{to}`
  - 复制 input / expectedOutput 字段；metadata 加 promoted_from / promoted_at / promoted_reason
  - 目标 item.id 用 sync_dataset 共享算法 compute_item_id 复算（保证同 vars 幂等）
  - 源 item 不删除（让 eval-online 下次跑时自动覆盖清理）
"""

from __future__ import annotations

from datetime import datetime, timezone

import click

from eval_shared.common.config import init_env
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.langfuse_client import LangfuseClient


_VALID_TARGETS = ("golden", "regression")


def _target_dataset_name(agent: str, to: str) -> str:
    return f"{agent}-{to}"


def _source_dataset_name(agent: str, from_arg: str | None) -> str:
    return from_arg or f"{agent}-online-temp"


def _summarize_input(value: object, max_len: int = 60) -> str:
    """把 item.input 压成单行短摘要，给 --list 视图用。"""
    import json
    if isinstance(value, str):
        s = value
    else:
        s = json.dumps(value, ensure_ascii=False, default=str)
    s = s.replace("\n", " ").strip()
    return s[:max_len] + "…" if len(s) > max_len else s


def _list_items(client: LangfuseClient, dataset_name: str) -> None:
    items = client.get_dataset_items(dataset_name)
    click.echo(f"📋 dataset `{dataset_name}` 共 {len(items)} 条 item：\n")
    for it in items:
        item_id = it.get("id", "?")
        input_short = _summarize_input(it.get("input"))
        meta = it.get("metadata") or {}
        score = meta.get("score_value")
        score_str = f"  score={score}" if score is not None else ""
        click.echo(f"  {item_id}{score_str}\n    input: {input_short}\n")


@click.command()
@click.option("--agent", required=True, help="Agent 名称（如 intention）")
@click.option(
    "--from",
    "from_dataset",
    default=None,
    help="源 dataset 名（默认 {agent}-online-temp）",
)
@click.option(
    "--to",
    "to_kind",
    default="golden",
    type=click.Choice(_VALID_TARGETS, case_sensitive=False),
    help="目标 dataset 类别（默认 golden）",
)
@click.option(
    "--item-ids",
    default="",
    help="要 promote 的 item id（逗号分隔）。与 --list 二选一。",
)
@click.option(
    "--reason",
    default="",
    help="promote 原因，写入目标 item 的 metadata 用于审计",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    help="列出源 dataset 的所有 item 帮你挑（不执行 promote）",
)
@click.option("--dry-run", is_flag=True, help="只打印将要做的操作，不写入 Langfuse")
def main(
    agent: str,
    from_dataset: str | None,
    to_kind: str,
    item_ids: str,
    reason: str,
    list_only: bool,
    dry_run: bool,
):
    """把 online-temp 的某些 item promote 到 golden 或 regression。"""
    init_env()

    source = _source_dataset_name(agent, from_dataset)
    target = _target_dataset_name(agent, to_kind)

    click.echo(f"📂 源 : {source}")
    click.echo(f"📂 目标: {target}")
    if dry_run:
        click.echo("⚠️  DRY-RUN 模式，不会实际写入。")
    click.echo("")

    with LangfuseClient() as client:
        if list_only:
            _list_items(client, source)
            return

        ids = [x.strip() for x in item_ids.split(",") if x.strip()]
        if not ids:
            raise click.ClickException(
                "必须提供 --item-ids（逗号分隔），或用 --list 先查看可选 item"
            )

        # 确保目标 dataset 存在
        if not dry_run and not client.dataset_exists(target):
            client.create_dataset(
                target,
                f"Auto-created by eval-dataset-promote on {datetime.now(timezone.utc).isoformat()}",
            )
            click.echo(f"✨ 新建目标 dataset {target}")

        promoted_at = datetime.now(timezone.utc).isoformat()
        ok = fail = 0

        for item_id in ids:
            try:
                src_item = client.get_dataset_item(item_id)
            except Exception as e:
                click.echo(f"  ❌ 拉取源 item 失败 {item_id}: {e}", err=True)
                fail += 1
                continue

            src_input = src_item.get("input")
            src_expected = src_item.get("expectedOutput")
            src_meta = src_item.get("metadata") or {}

            target_meta = {
                **src_meta,
                "promoted_from": source,
                "promoted_from_item_id": src_item.get("id"),
                "promoted_at": promoted_at,
                "promoted_reason": reason,
            }

            # 用 sync_dataset 同一算法计算目标 id，让后续 sync 幂等
            target_item_id = (
                compute_item_id(target, src_input)
                if isinstance(src_input, dict)
                else None
            )

            body: dict[str, object] = {
                "datasetName": target,
                "input": src_input,
                "expectedOutput": src_expected,
                "metadata": target_meta,
            }
            if target_item_id:
                body["id"] = target_item_id

            if dry_run:
                click.echo(
                    f"  DRY ✓ {item_id} → {target} "
                    f"(target_id={target_item_id or 'auto'})"
                )
                ok += 1
                continue

            try:
                client.upsert_dataset_item(body)
                click.echo(
                    f"  ✅ {item_id} → {target} "
                    f"(target_id={target_item_id or 'auto'})"
                )
                ok += 1
            except Exception as e:
                click.echo(f"  ❌ promote 失败 {item_id}: {e}", err=True)
                fail += 1

        click.echo("")
        click.echo(f"汇总: 成功 {ok} / 失败 {fail}")
        if fail > 0:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
