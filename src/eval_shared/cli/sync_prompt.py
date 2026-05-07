"""
eval-sync-prompt — Langfuse Prompt ↔ 本地 prompt.yaml 双向同步。

用法：
  eval-sync-prompt --agent <name> [--direction pull|push] [--label <label>]
  eval-sync-prompt --all           [--direction pull|push] [--label <label>]
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click
import httpx

from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient
from eval_shared.common.yaml_utils import load_yaml, dump_yaml


def _discover_agents() -> list[str]:
    agents_dir = Path.cwd() / "agents"
    if not agents_dir.exists():
        return []
    return sorted(
        d.name
        for d in agents_dir.iterdir()
        if d.is_dir() and (d / "prompt.yaml").exists()
    )


def _pull(client: LangfuseClient, agent: str, label: str | None) -> None:
    prompt_name = f"{agent}-prompt"
    label_info = f" (label={label})" if label else " (latest production)"
    click.echo(f"📥 [{agent}] GET {prompt_name}{label_info}")

    try:
        data = client.get_prompt(prompt_name, label=label)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response else ""
        raise click.ClickException(
            f"拉取失败：{e.response.status_code} {e.response.reason_phrase}"
            f"{f' — {body}' if body else ''}"
        )

    if data.get("type") != "chat":
        raise click.ClickException(f"暂不支持 prompt 类型：{data.get('type')}（仅支持 chat）")

    messages = data.get("prompt")
    if not isinstance(messages, list):
        raise click.ClickException("Langfuse 返回体 prompt 字段非数组（chat 格式应为消息数组）")

    out_path = Path.cwd() / "agents" / agent / "prompt.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    labels_str = ", ".join(data.get("labels", [])) or "(无)"
    header = "\n".join([
        "# 由 eval-sync-prompt 从 Langfuse 同步生成，请勿手动修改。",
        "# 迭代 Prompt 请在 Langfuse UI 中操作，再执行 npm run sync:prompts:pull",
        f"# 来源  : {prompt_name}",
        f"# 版本  : v{data.get('version', '?')}",
        f"# 标签  : {labels_str}",
        f"# 时间  : {datetime.now(timezone.utc).isoformat()}",
        "",
        "",
    ])

    dump_yaml(messages, out_path, header=header)
    rel = out_path.relative_to(Path.cwd())
    click.echo(
        f"   ✅ v{data.get('version')}  msgs={len(messages)}"
        f"  labels={labels_str}  →  {rel}"
    )


def _push(client: LangfuseClient, agent: str, label: str | None) -> None:
    prompt_name = f"{agent}-prompt"
    file_path = Path.cwd() / "agents" / agent / "prompt.yaml"
    if not file_path.exists():
        raise click.ClickException(f"文件不存在：{file_path}")

    messages = load_yaml(file_path)
    if not isinstance(messages, list):
        raise click.ClickException("prompt.yaml 格式错误：期望 chat 消息数组 [{role, content}, ...]")

    body: dict = {"name": prompt_name, "type": "chat", "prompt": messages}
    if label:
        body["labels"] = [label]

    label_info = f"  label={label}" if label else ""
    click.echo(f"📤 [{agent}] POST {prompt_name}  msgs={len(messages)}{label_info}")

    try:
        result = client.create_prompt(body)
    except httpx.HTTPStatusError as e:
        text = e.response.text[:300] if e.response else ""
        raise click.ClickException(
            f"上传失败：{e.response.status_code} {e.response.reason_phrase}"
            f"{f' — {text}' if text else ''}"
        )

    labels_str = ", ".join(result.get("labels", [])) or "(无)"
    click.echo(f"   ✅ v{result.get('version')}  labels={labels_str}")


@click.command()
@click.option("--agent", default=None, help="Agent 名称")
@click.option("--all", "sync_all", is_flag=True, help="同步所有 Agent")
@click.option(
    "--direction",
    type=click.Choice(["pull", "push"]),
    default="pull",
    help="同步方向（默认 pull）",
)
@click.option("--label", default=None, help="Prompt 标签（pull 时指定版本，push 时附加标签）")
def main(agent: str | None, sync_all: bool, direction: str, label: str | None):
    """Langfuse Prompt ↔ 本地 prompt.yaml 双向同步。"""
    if not agent and not sync_all:
        click.echo(
            "用法：eval-sync-prompt --agent <name> [--direction pull|push] [--label <label>]\n"
            "      eval-sync-prompt --all [--direction pull|push] [--label <label>]",
            err=True,
        )
        raise SystemExit(1)

    init_env()
    agents = _discover_agents() if sync_all else [agent]
    if not agents:
        raise click.ClickException("未发现任何 agent（请检查 agents/ 目录，或显式传 --agent）")

    action = "拉取" if direction == "pull" else "上传"
    click.echo(f"🔄 {action} Prompt")
    click.echo(f"   Agents   : {', '.join(agents)}")
    if label:
        click.echo(f"   Label    : {label}")
    click.echo("")

    failures = []
    with LangfuseClient() as client:
        for ag in agents:
            try:
                if direction == "pull":
                    _pull(client, ag, label)
                else:
                    _push(client, ag, label)
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
