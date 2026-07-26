"""
eval-promote — 将 Langfuse Prompt 从 staging 提升为 production。

用法：
  eval-promote --agent <agent-name> [--dry-run]

A/B 门禁（基于 ABVerdict 三态枚举）：
  ✅ A/B ✅ → 放行
  ⚠️ A/B 🟰 → 警告但放行（候选与基线相当，不阻塞 promote）
  ❌ A/B ❌ → 阻断；--force 可绕过

标签剥离（graveyard 方案，#10/#21）：
  Langfuse `newLabels` 只增/移动、不删除，无法直接从版本上摘标签。
  promote 后回读校验 production 落点，再把残留的 A/B 评估状态标签
  "移动"到最老的非本版本（graveyard），使 production 版本不带评估状态。
  staging 标签无法删除，留在原版本，下次 sync push 时被自然移走。
  注意：graveyard 取「最老的非本版本」——若不存在更老版本，标签会落到更新的
  版本上；该版本日后若成为 staging 且带 `A/B ❌`，会触发 promote 阻断门
  （属预期保守行为，确认无误可 --force）。
"""

from __future__ import annotations

import click
import httpx

from eval_shared.common.ab_verdict import AB_VERDICT_LABELS, ABVerdict
from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


def _is_ab_state_label(label: str) -> bool:
    """A/B 评估状态标签：新枚举（A/B ✅/❌/🟰），或旧的数字明细格式（兼容历史 prompt）。

    promote 时这些都从 production 版本上剥离，避免评估状态污染生产版本。
    """
    return label in AB_VERDICT_LABELS or label.startswith("A/B ")


def _is_blocking_verdict(label: str) -> bool:
    """识别 A/B 失败标签（精确枚举或旧 'A/B ❌ 67.7%→...' 前缀格式）。"""
    return label == ABVerdict.WORSE.value or label.startswith("A/B ❌")


@click.command()
@click.option("--agent", required=True, help="Agent 名称")
@click.option("--dry-run", is_flag=True, help="只查询当前 staging 版本，不执行变更")
@click.option(
    "--force",
    is_flag=True,
    help="跳过 A/B 门禁（即使 staging 带有 'A/B ❌' 标签也执行 promote）",
)
def main(agent: str, dry_run: bool, force: bool):
    """将 Langfuse Prompt 从 staging 提升为 production。"""
    init_env()

    prompt_name = f"{agent}-prompt"
    click.echo(f"🚀 提升 Prompt：{prompt_name}")
    click.echo("   staging → production")

    if dry_run:
        click.echo("⚠️  Dry-run 模式，不会实际执行变更。")

    with LangfuseClient() as client:
        try:
            prompt = client.get_prompt(prompt_name, label="staging")
        except httpx.HTTPStatusError as e:
            raise click.ClickException(f"获取 staging Prompt 失败：{e.response.status_code}")

        version = prompt.get("version")
        if not version:
            raise click.ClickException("Langfuse 返回的 staging Prompt 缺少 version，无法提升")

        click.echo(f"   当前 staging 版本：{version}")

        existing_labels = prompt.get("labels", [])

        # A/B 门禁：staging 带有 'A/B ❌' 时，默认拒绝 promote（--force 可绕）
        failed_ab = [lb for lb in existing_labels if _is_blocking_verdict(lb)]
        if failed_ab and not force:
            raise click.ClickException(
                f"staging 版本带有失败的 A/B 标签：{failed_ab}。"
                "确认要继续请加 --force。"
            )
        if failed_ab and force:
            click.echo(f"⚠️  --force：忽略失败的 A/B 标签 {failed_ab}")

        # SAME（🟰）警告但不阻塞——候选与基线相当，让人知情后自行决定是否真的要 promote
        same_ab = [lb for lb in existing_labels if lb == ABVerdict.SAME.value]
        if same_ab:
            click.echo(
                f"⚠️  staging 带有 {same_ab}：候选与基线相当，本次 promote 可能无实质提升。"
            )

        if dry_run:
            click.echo("✅ Dry-run 完成，以上版本将被标记为 production。")
            return

        # newLabels 语义 = 只增/移动、不删除（踩坑记录见 AGENTS.md），
        # 因此这里只追加 production；A/B 状态标签靠下面的 graveyard 移动剥离。
        try:
            client.update_prompt_labels(prompt_name, version, ["production"])
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300] if e.response else ""
            raise click.ClickException(
                f"标记 production 失败：{e.response.status_code}"
                f"{f' — {body}' if body else ''}"
            )

        # 回读校验（#21）：不信任 PATCH 返回值，读 production 实际落点
        try:
            check = client.get_prompt(prompt_name, label="production")
        except httpx.HTTPStatusError as e:
            raise click.ClickException(
                f"promote 后回读 production 失败：{e.response.status_code}"
            )
        if check.get("version") != version:
            raise click.ClickException(
                f"回读校验失败：production 实际在 v{check.get('version')}，"
                f"预期 v{version}——请在 Langfuse UI 核查"
            )

        # graveyard 移动（#10）：把残留的 A/B 评估状态标签移到最老的非本版本上
        stale = [lb for lb in check.get("labels", []) if _is_ab_state_label(lb)]
        if stale:
            meta = client.list_prompt_meta(prompt_name)
            other_versions = sorted(
                v for v in meta.get("versions", []) if v != version
            )
            if not other_versions:
                click.echo(
                    f"⚠️ 无其他版本可作 graveyard，A/B 标签 {stale} 仍留在 v{version}"
                    "（Langfuse 无删除标签 API，请 UI 手动清理）",
                    err=True,
                )
            else:
                graveyard = other_versions[0]
                client.update_prompt_labels(prompt_name, graveyard, stale)
                recheck = client.get_prompt(prompt_name, label="production")
                still = [lb for lb in recheck.get("labels", []) if _is_ab_state_label(lb)]
                if still:
                    click.echo(
                        f"❌ A/B 标签剥离失败，production 版本仍带 {still}"
                        "——请在 Langfuse UI 手动清理",
                        err=True,
                    )
                    raise SystemExit(1)
                click.echo(f"🪦 A/B 状态标签 {stale} 已移至 graveyard 版本 v{graveyard}")

        if "staging" in check.get("labels", []):
            click.echo(
                "ℹ️ staging 标签仍在本版本（Langfuse 不支持删除；"
                "下次 sync:prompts:push 会把它移到新版本）"
            )

        click.echo(f"✅ 已将 {prompt_name} v{version} 标记为 production（回读校验通过）")
        click.echo("⚠️ 契约提醒（PROTOCOL §2.3）：production 标签 = Dify 生产实际运行版。")
        click.echo("   请同步 Dify 节点 prompt，并跑 `npm run sync:prompts:pull` 复核一致。")


if __name__ == "__main__":
    main()
