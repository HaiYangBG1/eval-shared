"""
eval-dspy-optimize — DSPy Prompt 优化器 CLI 入口。

框架阶段：提供数据加载验证和脚手架，MIPROv2 闭环后续实现。

用法：
  eval-dspy-optimize --config <path> [--dry-run]
"""

from __future__ import annotations

from pathlib import Path

import click

from eval_shared.common.config import init_env
from eval_shared.common.yaml_utils import load_yaml


@click.command()
@click.option("--config", "config_path", required=True, help="优化配置文件路径（YAML）")
@click.option("--dry-run", is_flag=True, help="只加载数据不执行优化")
def main(config_path: str, dry_run: bool):
    """DSPy Prompt 优化器（框架阶段）。

    配置文件示例（dspy-optimize.yaml）：

    \b
      dataset: intention                # Langfuse Dataset 名称
      input_field: query                # 输入字段名
      output_field: answer              # 输出字段名
      prompt_name: intention-prompt     # 优化后上传的 Prompt 名称
      source: langfuse                  # 数据来源：langfuse 或 json
      json_path: output/xxx.json        # source=json 时的文件路径
    """
    init_env()

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise click.ClickException(f"配置文件不存在：{config_path}")

    config = load_yaml(cfg_file)
    if not config:
        raise click.ClickException("配置文件为空")

    dataset = config.get("dataset", "")
    input_field = config.get("input_field", "query")
    output_field = config.get("output_field", "answer")
    source = config.get("source", "langfuse")

    click.echo("╔══════════════════════════════════════════════════╗")
    click.echo("║         eval-dspy-optimize · Prompt 优化         ║")
    click.echo("╚══════════════════════════════════════════════════╝")
    click.echo(f"  配置文件  : {config_path}")
    click.echo(f"  数据来源  : {source}")
    click.echo(f"  数据集    : {dataset or config.get('json_path', '?')}")
    click.echo(f"  输入字段  : {input_field}")
    click.echo(f"  输出字段  : {output_field}")
    click.echo(f"  模式      : {'🧪 DRY-RUN' if dry_run else '🚀 正式运行'}")
    click.echo("")

    # 加载数据
    try:
        from eval_shared.dspy.loader import load_from_langfuse, load_from_json
    except ImportError:
        raise click.ClickException(
            "dspy 未安装，请运行: pip install eval-shared[dspy]"
        )

    if source == "json":
        json_path = config.get("json_path")
        if not json_path:
            raise click.ClickException("source=json 时必须指定 json_path")
        examples = load_from_json(json_path, input_field, output_field)
    else:
        if not dataset:
            raise click.ClickException("source=langfuse 时必须指定 dataset")
        examples = load_from_langfuse(dataset, input_field, output_field)

    click.echo(f"✅ 成功加载 {len(examples)} 条 Example")

    if examples:
        click.echo(f"   示例 #1: {examples[0]}")
    click.echo("")

    if dry_run:
        click.echo("🧪 DRY-RUN 完成，数据加载验证通过。")
        return

    # TODO: 后续实现 MIPROv2 优化闭环
    click.echo("⚠️  MIPROv2 优化功能正在开发中。")
    click.echo("   当前可用步骤：")
    click.echo("   1. ✅ 数据加载验证（已完成）")
    click.echo("   2. ⬜ DSPy Module 定义")
    click.echo("   3. ⬜ Metric 定义")
    click.echo("   4. ⬜ MIPROv2 优化运行")
    click.echo("   5. ⬜ 优化结果上传 Langfuse")


if __name__ == "__main__":
    main()
