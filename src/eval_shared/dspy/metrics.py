"""
DSPy 评估指标集合。

提供三种内置指标：
  - exact_match:  所有输出字段严格匹配
  - contains:     预期输出包含在预测输出中
  - llm_judge:    使用 LLM 作为评委打分（支持 rubric_file 引用 eval-specs）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


def _extract_rubric_from_file(
    rubric_file: str,
    section_name: str = "打分规则",
) -> str:
    """
    从 eval-spec 文件中提取打分规则章节。

    与 eval-online 使用相同的提取逻辑，确保 Single Source of Truth。

    Args:
        rubric_file: eval-spec 文件路径（如 docs/eval-specs/recommend.md）
        section_name: 章节名称（默认"打分规则"）

    Returns:
        提取出的打分规则文本
    """
    path = Path(rubric_file)
    if not path.exists():
        raise FileNotFoundError(f"rubric_file 不存在: {rubric_file}")

    full_content = path.read_text("utf-8")
    pattern = rf"^##\s+\d+\.\s*{re.escape(section_name)}"
    m = re.search(pattern, full_content, re.MULTILINE)
    if m:
        return full_content[m.start():]
    else:
        return full_content


def make_exact_match_metric(output_fields: list[str]) -> Callable:
    """
    创建精确匹配指标。

    所有输出字段的预测值（strip 后）必须与标注值完全一致才得分。

    Args:
        output_fields: 需要比较的输出字段名列表

    Returns:
        DSPy metric 函数 (example, pred, trace) -> float
    """

    def metric(example: Any, pred: Any, trace: Any = None) -> float:
        for field in output_fields:
            expected = str(getattr(example, field, "")).strip()
            predicted = str(getattr(pred, field, "")).strip()
            if predicted != expected:
                return 0.0
        return 1.0

    metric.__name__ = "exact_match"
    return metric


def make_contains_metric(output_fields: list[str]) -> Callable:
    """
    创建包含匹配指标。

    预期输出文本被包含在预测输出中即得分。

    Args:
        output_fields: 需要比较的输出字段名列表

    Returns:
        DSPy metric 函数 (example, pred, trace) -> float
    """

    def metric(example: Any, pred: Any, trace: Any = None) -> float:
        for field in output_fields:
            expected = str(getattr(example, field, "")).strip()
            predicted = str(getattr(pred, field, "")).strip()
            if expected and expected not in predicted:
                return 0.0
        return 1.0

    metric.__name__ = "contains"
    return metric


def make_llm_judge_metric(
    rubric: str,
    output_fields: list[str],
    threshold: float = 0.5,
) -> Callable:
    """
    创建 LLM-as-Judge 指标。

    使用当前配置的 DSPy LM 作为评委，根据 rubric 对预测结果打分。
    rubric 中的 {{input}} / {{output}} 占位符会被替换为实际数据。

    Args:
        rubric: 评分标准描述（可来自 eval-spec 文件）
        output_fields: 需要评估的输出字段名列表
        threshold: 通过阈值（0-1），默认 0.5（二值评分时 ≥0.5 即通过）

    Returns:
        DSPy metric 函数 (example, pred, trace) -> float
    """
    import dspy

    class JudgeSignature(dspy.Signature):
        """根据评分标准（rubric）对 AI 预测结果打分。"""
        prediction = dspy.InputField(desc="AI 的预测输出")
        reference = dspy.InputField(desc="期望的参考答案")
        rubric = dspy.InputField(desc="评分标准")
        score = dspy.OutputField(desc="0 或 1 的分数，1 表示合格")

    judge = dspy.Predict(JudgeSignature)

    def metric(example: Any, pred: Any, trace: Any = None) -> float:
        pred_text = " | ".join(
            f"{f}={getattr(pred, f, '')}" for f in output_fields
        )
        ref_text = " | ".join(
            f"{f}={getattr(example, f, '')}" for f in output_fields
        )

        # 如果 rubric 中有 {{input}} / {{output}} 占位符，替换为实际值
        # 这样 DSPy 可以复用 eval-specs 中的同一份 rubric 模板
        input_fields = [f for f in dir(example) if not f.startswith("_")
                        and f not in output_fields]
        input_text = " | ".join(
            f"{f}={getattr(example, f, '')}" for f in input_fields
            if hasattr(example, f)
        )

        actual_rubric = rubric.replace("{{input}}", input_text).replace(
            "{{output}}", pred_text
        )

        try:
            result = judge(
                prediction=pred_text,
                reference=ref_text,
                rubric=actual_rubric,
            )
            score_val = float(result.score)
            return 1.0 if score_val >= threshold else 0.0
        except (ValueError, TypeError, AttributeError):
            return 0.0

    metric.__name__ = "llm_judge"
    return metric


def build_metric(metric_config: dict, output_fields: list[str]) -> Callable:
    """
    根据配置构建评估指标。

    支持两种 rubric 来源：
      - rubric: 内联文本
      - rubric_file: 引用 eval-spec 文件（Single Source of Truth）

    Args:
        metric_config: YAML 中的 metric 段：
            {"type": "exact_match"} 或
            {"type": "llm_judge", "rubric_file": "docs/eval-specs/recommend.md"}
        output_fields: 输出字段名列表

    Returns:
        DSPy metric 函数
    """
    metric_type = metric_config.get("type", "exact_match")

    if metric_type == "exact_match":
        return make_exact_match_metric(output_fields)
    elif metric_type == "contains":
        return make_contains_metric(output_fields)
    elif metric_type == "llm_judge":
        # 优先使用 rubric_file（Single Source），回退到内联 rubric
        rubric_file = metric_config.get("rubric_file")
        if rubric_file:
            section = metric_config.get("rubric_section", "打分规则")
            rubric = _extract_rubric_from_file(rubric_file, section)
        else:
            rubric = metric_config.get("rubric", "评估预测输出与参考答案的语义一致性。")

        threshold = metric_config.get("threshold", 0.5)
        return make_llm_judge_metric(rubric, output_fields, threshold)
    else:
        raise ValueError(f"不支持的 metric 类型：{metric_type}（可选：exact_match, contains, llm_judge）")
