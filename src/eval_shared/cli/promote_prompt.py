"""
eval-promote — 将 Langfuse Prompt 从 staging 提升为 production。

用法：
  eval-promote --agent <agent-name> [--dry-run]
"""

from __future__ import annotations

import click
import httpx

from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


@click.command()
@click.option("--agent", required=True, help="Agent 名称")
@click.option("--dry-run", is_flag=True, help="只查询当前 staging 版本，不执行变更")
def main(agent: str, dry_run: bool):
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

        click.echo(f"   当前 staging 版本：{prompt.get('version')}")

        if dry_run:
            click.echo("✅ Dry-run 完成，以上版本将被标记为 production。")
            return

        # Langfuse API 自动标记 production 功能需根据 API 版本适配
        click.echo("⚠️  自动标记 production 功能需根据 Langfuse API 版本适配。")
        click.echo("   请手动在 Langfuse UI 中将上述版本标记为 production。")


if __name__ == "__main__":
    main()
