"""
eval-promote — 将 Langfuse Prompt 从 staging 提升为 production。

用法：
  eval-promote --agent <agent-name> [--dry-run]

A/B 门禁（基于 ABVerdict 三态枚举）：
  ✅ A/B ✅ → 放行
  ⚠️ A/B 🟰 → 警告但放行（候选与基线相当，不阻塞 promote）
  ❌ A/B ❌ → 阻断；--force 可绕过
  promote 时 production 版本上不保留任何 A/B 评估状态 label
"""

from __future__ import annotations

import click
import httpx

from eval_shared.common.ab_verdict import AB_VERDICT_LABELS, ABVerdict
from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


# latest 由 Langfuse 系统管理；staging/production 是流程语义上互斥的标签，
# promote 时统一从原 labels 中剔除，再追加 production。
_RESERVED_LABELS = {"latest", "production", "staging"}


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

        # 剥离全部保留 label + 全部 A/B 评估状态 label，让 production 版本只带流程标签
        labels = [
            lb for lb in existing_labels
            if lb not in _RESERVED_LABELS and not _is_ab_state_label(lb)
        ]
        labels.append("production")

        try:
            client.update_prompt_labels(prompt_name, version, labels)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300] if e.response else ""
            raise click.ClickException(
                f"标记 production 失败：{e.response.status_code}"
                f"{f' — {body}' if body else ''}"
            )

        click.echo(f"✅ 已将 {prompt_name} v{version} 标记为 production")


if __name__ == "__main__":
    main()
