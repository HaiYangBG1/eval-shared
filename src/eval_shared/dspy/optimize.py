"""
eval-dspy-optimize — DSPy Prompt 优化器 CLI。

完整流水线：
  1. 加载配置文件（YAML）
  2. 配置 DSPy LM
  3. 动态创建 Signature 和 Module
  4. 加载数据集并分割 train/dev
  5. 基线评估（优化前）
  6. 运行优化器（MIPROv2 / BootstrapFewShot / BootstrapFewShotWithRandomSearch）
  7. 优化后评估
  8. 对比结果
  9. 保存优化状态
  10. 上传到 Langfuse（可选）

用法：
  eval-dspy-optimize --config <path> [--dry-run]
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import click

from eval_shared.common.config import init_env
from eval_shared.common.yaml_utils import load_yaml


def _configure_dspy_lm() -> None:
    """配置 DSPy LM（从环境变量读取）。"""
    import dspy
    from eval_shared.common.config import get_dspy_lm_config

    cfg = get_dspy_lm_config()
    lm = dspy.LM(
        model=cfg["model"],
        api_base=cfg["api_base"],
        api_key=cfg["api_key"],
        temperature=0.0,  # 优化阶段用 0，减少随机性
    )
    dspy.configure(lm=lm)
    click.echo(f"  LM        : {cfg['model']}")
    click.echo(f"  API Base  : {cfg['api_base']}")


def _load_examples(config: dict) -> list:
    """根据配置加载 dspy.Example 列表。"""
    from eval_shared.dspy.loader import load_from_langfuse, load_from_json

    source = config.get("source", "langfuse")
    task = config.get("task", {})

    # 从任务配置中获取第一个输入/输出字段名
    input_fields = task.get("input_fields", [{"name": "query"}])
    output_fields = task.get("output_fields", [{"name": "answer"}])
    input_field = input_fields[0]["name"]
    output_field = output_fields[0]["name"]

    if source == "json":
        json_path = config.get("json_path")
        if not json_path:
            raise click.ClickException("source=json 时必须指定 json_path")
        return load_from_json(json_path, input_field, output_field)
    else:
        dataset_name = config.get("dataset", "")
        if not dataset_name:
            raise click.ClickException("source=langfuse 时必须指定 dataset")
        return load_from_langfuse(dataset_name, input_field, output_field)


def _split_data(examples: list, train_ratio: float = 0.7) -> tuple[list, list]:
    """分割训练集和验证集。"""
    random.shuffle(examples)
    split_idx = max(1, int(len(examples) * train_ratio))
    return examples[:split_idx], examples[split_idx:]


def _run_evaluation(module, dev_data: list, metric, label: str = "") -> float:
    """运行评估并返回分数。"""
    import dspy

    evaluator = dspy.Evaluate(
        devset=dev_data,
        metric=metric,
        num_threads=1,
        display_progress=True,
        display_table=5,  # 显示前 5 条结果
    )
    score = evaluator(module)
    if label:
        click.echo(f"\n  {label}: {score:.2%}")
    return score


def _run_optimizer(config: dict, module, trainset: list, metric):
    """运行优化器。"""
    from dspy.teleprompt import MIPROv2, BootstrapFewShot

    opt_config = config.get("optimizer", {})
    opt_type = opt_config.get("type", "miprov2")
    max_bootstrapped = opt_config.get("max_bootstrapped_demos", 3)
    max_labeled = opt_config.get("max_labeled_demos", 3)

    if opt_type == "bootstrap_fewshot":
        click.echo("  优化器    : BootstrapFewShot")
        optimizer = BootstrapFewShot(
            metric=metric,
            max_bootstrapped_demos=max_bootstrapped,
            max_labeled_demos=max_labeled,
        )
        return optimizer.compile(module, trainset=trainset)

    elif opt_type == "bootstrap_random":
        click.echo("  优化器    : BootstrapFewShotWithRandomSearch")
        from dspy.teleprompt import BootstrapFewShotWithRandomSearch
        num_candidate = opt_config.get("num_candidate_programs", 8)
        optimizer = BootstrapFewShotWithRandomSearch(
            metric=metric,
            max_bootstrapped_demos=max_bootstrapped,
            max_labeled_demos=max_labeled,
            num_candidate_programs=num_candidate,
        )
        return optimizer.compile(module, trainset=trainset)

    else:
        # 默认 MIPROv2
        auto = opt_config.get("auto", "light")
        num_trials = opt_config.get("num_trials", 10)
        click.echo(f"  优化器    : MIPROv2 (auto={auto}, trials={num_trials})")
        optimizer = MIPROv2(
            metric=metric,
            auto=auto,
            max_bootstrapped_demos=max_bootstrapped,
            max_labeled_demos=max_labeled,
        )
        return optimizer.compile(module, trainset=trainset, num_trials=num_trials)


@click.command()
@click.option("--config", "config_path", required=True, help="优化配置文件路径（YAML）")
@click.option("--dry-run", is_flag=True, help="只加载数据和配置，不执行优化")
@click.option("--seed", type=int, default=42, help="随机种子（默认 42）")
def main(config_path: str, dry_run: bool, seed: int):
    """DSPy Prompt 优化器 — 自动寻找最佳 Prompt 和 Few-shot 示例。"""
    init_env()

    # 加载配置
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise click.ClickException(f"配置文件不存在：{config_path}")

    config = load_yaml(cfg_file)
    if not config:
        raise click.ClickException("配置文件为空")

    # ═══ Banner ═══
    click.echo("")
    click.echo("╔══════════════════════════════════════════════════╗")
    click.echo("║        eval-dspy-optimize · Prompt 优化          ║")
    click.echo("╚══════════════════════════════════════════════════╝")
    click.echo(f"  配置文件  : {config_path}")
    click.echo(f"  数据来源  : {config.get('source', 'langfuse')}")
    click.echo(f"  数据集    : {config.get('dataset', config.get('json_path', '?'))}")
    click.echo(f"  模式      : {'🧪 DRY-RUN' if dry_run else '🚀 正式运行'}")
    click.echo("")

    # ═══ 检查 DSPy 依赖 ═══
    try:
        import dspy  # noqa: F401
    except ImportError:
        raise click.ClickException(
            "dspy 未安装，请运行: pip install eval-shared[dspy]"
        )

    # 设置随机种子
    random.seed(seed)

    # ═══ Step 1: 配置 LM ═══
    click.echo("━━━ Step 1/7: 配置 LM ━━━")
    _configure_dspy_lm()
    click.echo("")

    # ═══ Step 2: 创建任务模块 ═══
    click.echo("━━━ Step 2/7: 创建任务模块 ━━━")
    from eval_shared.dspy.module_factory import (
        create_signature,
        create_module,
        get_field_names,
    )

    task_config = config.get("task", {})
    if not task_config:
        raise click.ClickException("配置中缺少 task 段")

    signature = create_signature(task_config)
    module_type = task_config.get("module", "predict")
    module = create_module(signature, module_type)

    input_names, output_names = get_field_names(task_config)
    click.echo(f"  任务描述  : {task_config.get('description', '?')}")
    click.echo(f"  输入字段  : {input_names}")
    click.echo(f"  输出字段  : {output_names}")
    click.echo(f"  模块类型  : {module_type}")
    click.echo("")

    # ═══ Step 3: 加载数据 ═══
    click.echo("━━━ Step 3/7: 加载数据 ━━━")
    examples = _load_examples(config)
    click.echo(f"  ✅ 加载 {len(examples)} 条 Example")

    if len(examples) < 5:
        raise click.ClickException(
            f"数据量太少（{len(examples)} 条），至少需要 5 条用于优化。"
            "建议 20-50 条获得最佳效果。"
        )

    if examples:
        click.echo(f"  示例 #1: {examples[0]}")
    click.echo("")

    # ═══ Step 4: 分割数据 ═══
    click.echo("━━━ Step 4/7: 分割数据 ━━━")
    split_config = config.get("split", {})
    train_ratio = split_config.get("train_ratio", 0.7)
    train_data, dev_data = _split_data(examples, train_ratio)
    click.echo(f"  训练集    : {len(train_data)} 条 ({train_ratio:.0%})")
    click.echo(f"  验证集    : {len(dev_data)} 条 ({1 - train_ratio:.0%})")
    click.echo("")

    # ═══ Step 5: 构建评估指标 ═══
    click.echo("━━━ Step 5/7: 构建评估指标 ━━━")
    from eval_shared.dspy.metrics import build_metric

    metric_config = config.get("metric", {"type": "exact_match"})
    metric = build_metric(metric_config, output_names)
    click.echo(f"  指标类型  : {metric_config.get('type', 'exact_match')}")
    click.echo("")

    if dry_run:
        click.echo("🧪 DRY-RUN 完成 — 数据加载、模块创建、指标构建验证通过。")
        click.echo("")
        click.echo("   后续步骤（去掉 --dry-run 后执行）：")
        click.echo("   6. 基线评估 → 运行优化 → 优化后评估")
        click.echo("   7. 保存结果 → 上传 Langfuse")
        return

    # ═══ Step 6: 基线评估 + 优化 + 优化后评估 ═══
    click.echo("━━━ Step 6/7: 评估与优化 ━━━")

    click.echo("\n📊 基线评估（优化前）...")
    from eval_shared.dspy.module_factory import create_module as _create_module
    baseline_module = _create_module(signature, module_type)
    baseline_score = _run_evaluation(baseline_module, dev_data, metric, "基线准确率")

    click.echo("\n🔧 运行优化器...")
    optimized_module = _run_optimizer(config, module, train_data, metric)

    click.echo("\n📊 优化后评估...")
    optimized_score = _run_evaluation(optimized_module, dev_data, metric, "优化后准确率")

    # 对比
    click.echo("")
    click.echo("╔══════════════════════════════════════╗")
    click.echo("║          优化结果对比                 ║")
    click.echo("╠══════════════════════════════════════╣")
    click.echo(f"║  基线准确率  : {baseline_score:>6.2%}               ║")
    click.echo(f"║  优化后准确率: {optimized_score:>6.2%}               ║")

    delta = optimized_score - baseline_score
    if delta > 0:
        click.echo(f"║  提升       : +{delta:.2%}  📈             ║")
    elif delta < 0:
        click.echo(f"║  下降       : {delta:.2%}  📉             ║")
    else:
        click.echo(f"║  无变化     : ±0.00%  ➡️              ║")
    click.echo("╚══════════════════════════════════════╝")
    click.echo("")

    # ═══ Step 7: 保存与上传 ═══
    click.echo("━━━ Step 7/7: 保存与上传 ━━━")

    output_config = config.get("output", {})

    # 保存优化状态
    save_path = output_config.get("save_path", "")
    if not save_path:
        dataset_name = config.get("dataset", "unknown")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        save_path = f"output/optimized_{dataset_name}_{timestamp}.json"

    save_file = Path(save_path)
    save_file.parent.mkdir(parents=True, exist_ok=True)
    optimized_module.save(str(save_file))
    click.echo(f"  ✅ 优化状态已保存: {save_path}")

    # 保存对比报告
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": str(cfg_file),
        "dataset": config.get("dataset", ""),
        "total_examples": len(examples),
        "train_size": len(train_data),
        "dev_size": len(dev_data),
        "baseline_score": baseline_score,
        "optimized_score": optimized_score,
        "delta": delta,
        "optimizer": config.get("optimizer", {}).get("type", "miprov2"),
        "metric": metric_config.get("type", "exact_match"),
    }
    report_path = save_file.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(f"  ✅ 对比报告已保存: {report_path}")

    # 上传到 Langfuse
    upload = output_config.get("upload_langfuse", False)
    prompt_name = output_config.get("prompt_name", "")

    if upload and prompt_name:
        label = output_config.get("label", "staging")
        click.echo(f"\n  📤 上传到 Langfuse: {prompt_name} (label={label})")
        from eval_shared.dspy.uploader import upload_from_module
        upload_from_module(optimized_module, prompt_name, label)
    elif upload and not prompt_name:
        click.echo("  ⚠️  upload_langfuse=true 但未指定 prompt_name，跳过上传")

    click.echo("")
    click.echo("🎉 优化完成！")
    if delta > 0:
        click.echo(f"   准确率从 {baseline_score:.2%} 提升到 {optimized_score:.2%} (+{delta:.2%})")
    click.echo(f"   优化状态: {save_path}")
    click.echo(f"   对比报告: {report_path}")


if __name__ == "__main__":
    main()
