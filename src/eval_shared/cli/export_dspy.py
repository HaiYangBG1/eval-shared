"""
eval-export-dspy — 将 Langfuse Dataset 导出为 DSPy Example 格式。

用法：
  eval-export-dspy --agent <agent-name> [--output <output-path>]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.config import init_env, require_env
from eval_shared.common.langfuse_client import LangfuseClient


@click.command()
@click.option("--agent", required=True, help="Agent 名称（= Langfuse Dataset 名称）")
@click.option("--output", "output_path", default=None, help="输出文件路径")
def main(agent: str, output_path: str | None):
    """将 Langfuse Dataset 导出为 DSPy Example JSON 格式。"""
    init_env()
    require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    if output_path is None:
        output_path = f"output/{agent}-dspy-examples.json"

    click.echo(f"📦 从 Langfuse 导出数据集：{agent}")

    with LangfuseClient() as client:
        items = client.get_dataset_items(agent, limit=200)

    examples = []
    for item in items:
        input_data = item.get("input", {})
        expected = item.get("expectedOutput")

        query = (
            input_data.get("query")
            or input_data.get("question")
            or json.dumps(input_data, ensure_ascii=False)
        )
        answer = (
            expected if isinstance(expected, str)
            else json.dumps(expected, ensure_ascii=False) if expected
            else ""
        )

        examples.append({
            "query": query,
            "answer": answer,
            "_metadata": {
                "source": "langfuse",
                "datasetItemId": item.get("id"),
                "exportedAt": datetime.now(timezone.utc).isoformat(),
            },
        })

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")

    click.echo(f"✅ 导出完成！{len(examples)} 条 Example → {output_path}")
    click.echo("")
    click.echo("在 DSPy 中使用：")
    click.echo("  import dspy, json")
    click.echo(f'  with open("{output_path}") as f:')
    click.echo("      data = json.load(f)")
    click.echo(
        '  examples = [dspy.Example(query=d["query"], answer=d["answer"])'
        '.with_inputs("query") for d in data]'
    )


if __name__ == "__main__":
    main()
