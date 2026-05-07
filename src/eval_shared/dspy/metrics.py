"""
DSPy 评估指标集合。

提供三种内置指标：
  - exact_match:  所有输出字段严格匹配
  - contains:     预期输出包含在预测输出中
  - llm_judge:    使用 LLM 作为评委打分
"""

from __future__ import annotations

from typing import Any, Callable


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
    threshold: float = 0.7,
) -> Callable:
    """
    创建 LLM-as-Judge 指标。

    使用当前配置的 DSPy LM 作为评委，根据 rubric 对预测结果打分。

    Args:
        rubric: 评分标准描述
        output_fields: 需要评估的输出字段名列表
        threshold: 通过阈值（0-1），默认 0.7

    Returns:
        DSPy metric 函数 (example, pred, trace) -> float
    """
    import dspy

    class JudgeSignature(dspy.Signature):
        """根据评分标准（rubric）对 AI 预测结果打分。"""
        prediction = dspy.InputField(desc="AI 的预测输出")
        reference = dspy.InputField(desc="期望的参考答案")
        rubric = dspy.InputField(desc="评分标准")
        score = dspy.OutputField(desc="0.0-1.0 之间的分数，1.0 表示完美")

    judge = dspy.Predict(JudgeSignature)

    def metric(example: Any, pred: Any, trace: Any = None) -> float:
        pred_text = " | ".join(
            f"{f}={getattr(pred, f, '')}" for f in output_fields
        )
        ref_text = " | ".join(
            f"{f}={getattr(example, f, '')}" for f in output_fields
        )
        try:
            result = judge(
                prediction=pred_text,
                reference=ref_text,
                rubric=rubric,
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

    Args:
        metric_config: YAML 中的 metric 段：
            {"type": "exact_match"} 或
            {"type": "llm_judge", "rubric": "...", "threshold": 0.7}
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
        rubric = metric_config.get("rubric", "评估预测输出与参考答案的语义一致性。")
        threshold = metric_config.get("threshold", 0.7)
        return make_llm_judge_metric(rubric, output_fields, threshold)
    else:
        raise ValueError(f"不支持的 metric 类型：{metric_type}（可选：exact_match, contains, llm_judge）")
