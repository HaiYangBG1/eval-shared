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
  - --to regression 默认同时回写本地 `agents/{agent}/datasets/regression.yaml`
    （本地 YAML=SSOT，契约 §2.3；用户话术经 PII 脱敏后入 git，Langfuse 镜像保持原文）
  - --to regression 时 observation input（消息数组）按 `agents/{agent}/datasets/var-mapping.yaml`
    解析为 dict 型模板变量再双写（契约 §2.3 regression vars 口径，#39 方案 A）；
    解析失败该条硬失败，不写半程
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.config import init_env
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.pii import scrub_user_content
from eval_shared.common.template_vars import (
    VarParseError,
    is_multi_turn,
    load_var_mapping,
    parse_obs_input,
    var_mapping_path,
)
from eval_shared.common.yaml_utils import load_yaml, dump_yaml


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


def _local_regression_path(agent: str) -> Path:
    """本地 regression SSOT 路径（与 sync_dataset._local_path 同约定）。"""
    return Path.cwd() / "agents" / agent / "datasets" / "regression.yaml"


def _write_local_regression(
    agent: str, dataset_name: str, new_entries: list[dict]
) -> Path:
    """把 promote 的条目合并进本地 regression.yaml（按 id 覆盖，其余追加）。"""
    path = _local_regression_path(agent)
    existing = load_yaml(path) if path.exists() else []
    if not isinstance(existing, list):
        existing = []

    index_by_id = {
        e.get("id"): i
        for i, e in enumerate(existing)
        if isinstance(e, dict) and e.get("id")
    }
    for entry in new_entries:
        eid = entry.get("id")
        if eid and eid in index_by_id:
            existing[index_by_id[eid]] = entry
        else:
            existing.append(entry)

    header = "\n".join([
        f'# regression SSOT（入 git）—— Langfuse "{dataset_name}" 仅为运行镜像（契约 §2.3）。',
        "# 写入：eval-dataset-promote --to regression（自动双写+脱敏）",
        f"# 恢复：eval-sync-dataset --agent {agent} --type regression --direction push",
        f"# 条目数  : {len(existing)}",
        f"# 同步时间: {datetime.now(timezone.utc).isoformat()}",
        "",
        "",
    ])
    dump_yaml(existing, path, header=header)
    return path


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
@click.option(
    "--local-write/--no-local-write",
    default=True,
    help="--to regression 时同时回写本地 regression.yaml（默认开启；关闭仅限演练场景，契约 §2.3）",
)
def main(
    agent: str,
    from_dataset: str | None,
    to_kind: str,
    item_ids: str,
    reason: str,
    list_only: bool,
    dry_run: bool,
    local_write: bool,
):
    """把 online-temp 的某些 item promote 到 golden 或 regression。"""
    init_env()

    source = _source_dataset_name(agent, from_dataset)
    target = _target_dataset_name(agent, to_kind)

    do_local_write = to_kind == "regression" and local_write and not list_only
    if do_local_write and not (Path.cwd() / "agents" / agent).is_dir():
        # 早失败：还没写 Langfuse 就拦下，避免「镜像有、SSOT 没有」的半程状态
        raise click.ClickException(
            f"本地目录 agents/{agent}/ 不存在（cwd={Path.cwd()}）。"
            "请在业务仓根目录运行；确属演练场景可用 --no-local-write 跳过本地回写。"
        )
    if to_kind == "regression" and not local_write and not list_only:
        click.echo("⚠️  已关闭本地回写——契约 §2.3 要求 regression 双写，仅演练场景可这么干。")

    mapping = None
    if to_kind == "regression" and not list_only:
        try:
            mapping = load_var_mapping(agent)
        except VarParseError as e:
            raise click.ClickException(str(e))
        if mapping is not None:
            click.echo(
                f"🗺  变量映射: {var_mapping_path(agent)}"
                f"（query_var={mapping['query_var']}, sections={len(mapping['sections'])}）"
            )

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
        local_entries: list[dict] = []
        pii_hits = 0

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

            # regression vars 口径（契约 §2.3，#39 方案 A）：消息数组 → dict 型模板变量。
            # 解析失败硬失败该条，Langfuse/本地都不写（不写半程、不猜测）
            promote_input = src_input
            src_multi_turn = False
            if to_kind == "regression":
                try:
                    promote_input = parse_obs_input(src_input, mapping)
                except VarParseError as e:
                    click.echo(f"  ❌ 模板变量解析失败 {item_id}: {e}", err=True)
                    fail += 1
                    continue
                src_multi_turn = is_multi_turn(src_input)
                if src_multi_turn:
                    click.echo(
                        f"  ⚠️ 多轮观测 {item_id}：历史轮已丢弃（契约 §2.3④，"
                        "回放语义弱于原始现场，trace 可回溯）"
                    )

            target_meta = {
                **src_meta,
                "promoted_from": source,
                "promoted_from_item_id": src_item.get("id"),
                "promoted_at": promoted_at,
                "promoted_reason": reason,
            }
            if src_multi_turn:
                target_meta["multi_turn"] = True

            # 用 sync_dataset 同一算法计算目标 id，让后续 sync/重复 promote 幂等。
            # regression 的 id 基于解析后的 dict vars（契约 §2.3）；golden 保持原行为，
            # list 型 input 也必须算 id，否则 id=None 时 Langfuse 每次分配随机 id（#17）
            target_item_id = (
                compute_item_id(target, promote_input)
                if isinstance(promote_input, (dict, list))
                else None
            )

            body: dict[str, object] = {
                "datasetName": target,
                "input": promote_input,
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
            else:
                try:
                    resp = client.upsert_dataset_item(body)
                    click.echo(
                        f"  ✅ {item_id} → {target} "
                        f"(target_id={target_item_id or 'auto'})"
                    )
                    ok += 1
                    if not target_item_id and isinstance(resp, dict):
                        target_item_id = resp.get("id")
                except Exception as e:
                    click.echo(f"  ❌ promote 失败 {item_id}: {e}", err=True)
                    fail += 1
                    continue

            if do_local_write:
                # 本地 SSOT 条目：Langfuse 镜像保持原文，入 git 的用户话术按契约 §2.3 脱敏
                scrubbed_input, pii_changes = scrub_user_content(promote_input)
                for path_str, before, after in pii_changes:
                    click.echo(f"  🔒 PII {path_str}: {before!r} → {after!r}")
                pii_hits += len(pii_changes)

                src_assert = src_meta.get("assert")
                local_entry: dict = {
                    "id": target_item_id,
                    "vars": scrubbed_input,
                    "assert": src_assert if isinstance(src_assert, list) else [],
                }
                if src_expected:
                    local_entry["expectedOutput"] = src_expected
                # assert/index 不入 metadata（与 sync_dataset pull 对称，防往返 diff 噪音）
                local_entry["metadata"] = {
                    k: v for k, v in target_meta.items() if k not in ("assert", "index")
                }
                local_entries.append(local_entry)

        if do_local_write and local_entries:
            click.echo("")
            click.echo(f"🔒 PII 扫描：命中 {pii_hits} 处（0 = 无需脱敏）")
            if dry_run:
                click.echo(
                    f"  DRY 本地回写 {len(local_entries)} 条 → {_local_regression_path(agent)}"
                )
            else:
                local_path = _write_local_regression(agent, target, local_entries)
                click.echo(f"📝 本地 SSOT 已回写 {len(local_entries)} 条 → {local_path}")

        click.echo("")
        click.echo(f"汇总: 成功 {ok} / 失败 {fail}")
        if to_kind == "golden" and ok and not dry_run:
            click.echo(
                "ℹ️  golden 本地 SSOT 不自动回写——断言需人工设计，"
                "记得把 case 编入 agents/<agent>/datasets/golden.yaml（契约 §2.3）"
            )
        if fail > 0:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
