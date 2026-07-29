from __future__ import annotations

from eval_shared.cli.promptfoo_ab import _vars_key


def test_vars_key_ignores_trailing_whitespace_in_string_values() -> None:
    """YAML 折叠标量给 menu_data 带尾换行，promptfoo 返回值无：两者必须同 key。"""
    golden = {"query": "我一人吃", "menu_data": '[{"name":"米饭"}]\n'}
    result = {"query": "我一人吃", "menu_data": '[{"name":"米饭"}]'}
    assert _vars_key(golden) == _vars_key(result)


def test_vars_key_ignores_internal_json_whitespace() -> None:
    """YAML 折叠产生 `}, {"`、promptfoo 链路紧凑化为 `},{"`——JSON 值须规范化后匹配。"""
    golden = {"menu_data": '[{"a":1}, {"b":2}]\n'}
    result = {"menu_data": '[{"a":1},{"b":2}]'}
    assert _vars_key(golden) == _vars_key(result)


def test_vars_key_still_distinguishes_different_content() -> None:
    assert _vars_key({"query": "a"}) != _vars_key({"query": "b"})
    assert _vars_key({"m": '[{"a":1}]'}) != _vars_key({"m": '[{"a":2}]'})

from eval_shared.cli.promptfoo_ab import (
    _build_run_metadata,
    _downgrade_scoreless_hits_to_miss,
    _infer_local_dataset_path,
    _merge_hit_and_miss,
    _vars_key,
    _write_langfuse_run,
    calc_stats,
    find_regressions_and_improvements,
    generate_ab_report,
    is_safe_to_upgrade,
)
from eval_shared.common.dataset_item_id import compute_item_id
from eval_shared.common.dataset_run_cache import CacheLookupResult


def test_no_change_is_not_marked_safe_when_using_net_improvement_gate() -> None:
    baseline = [{"vars": {"query": "same"}, "success": True}]
    candidate = [{"vars": {"query": "same"}, "success": True}]
    base_stats = calc_stats(baseline)
    cand_stats = calc_stats(candidate)
    regressions, improvements = find_regressions_and_improvements(baseline, candidate)

    report = generate_ab_report(
        "agent",
        "production",
        "staging",
        base_stats,
        cand_stats,
        regressions,
        improvements,
        "base.json",
        "cand.json",
    )

    assert is_safe_to_upgrade(base_stats, cand_stats, regressions, improvements) is False
    assert "✅ **安全升级**" not in report
    assert "无明显改善" in report


def test_vars_key_is_independent_of_dict_order() -> None:
    assert _vars_key({"a": 1, "b": 2}) == _vars_key({"b": 2, "a": 1})


# ── _infer_local_dataset_path ──


def test_infer_local_path_for_golden() -> None:
    """约定：{agent}-{type} → agents/{agent}/datasets/{type}.yaml。"""
    from pathlib import Path
    assert _infer_local_dataset_path("intention", "intention-golden") == Path(
        "agents/intention/datasets/golden.yaml"
    )


def test_infer_local_path_for_regression() -> None:
    from pathlib import Path
    assert _infer_local_dataset_path("intention", "intention-regression") == Path(
        "agents/intention/datasets/regression.yaml"
    )


def test_infer_local_path_falls_back_to_full_name_when_no_agent_prefix() -> None:
    """自定义 dataset 名（不带 {agent}- 前缀）直接用原名作为文件名。"""
    from pathlib import Path
    assert _infer_local_dataset_path("intention", "custom-experiment") == Path(
        "agents/intention/datasets/custom-experiment.yaml"
    )


# ── _merge_hit_and_miss ──


def _full_dataset_3() -> list:
    return [
        {"vars": {"q": "a"}, "assert": []},
        {"vars": {"q": "b"}, "assert": []},
        {"vars": {"q": "c"}, "assert": []},
    ]


def test_merge_uses_hit_score_for_cached_cases_and_records_trace_id() -> None:
    full = _full_dataset_3()
    id_a = compute_item_id("ds", {"q": "a"})
    id_b = compute_item_id("ds", {"q": "b"})

    hits = {id_a: "trc-A", id_b: "trc-B"}
    hit_scores = {"trc-A": 1.0, "trc-B": 0.0}
    miss_results = [{"vars": {"q": "c"}, "success": True}]

    results, id_to_trace = _merge_hit_and_miss(
        full_dataset=full,
        dataset_name="ds",
        hits=hits,
        hit_scores=hit_scores,
        miss_results=miss_results,
    )

    assert [(r["vars"]["q"], r["success"], r["_cache_hit"]) for r in results] == [
        ("a", True, True),
        ("b", False, True),
        ("c", True, False),
    ]
    assert id_to_trace == {id_a: "trc-A", id_b: "trc-B"}


def test_merge_treats_missing_hit_score_as_fail() -> None:
    """hit case 但 score 拉不到（如 source run 被并发删）→ 保守判 fail。"""
    full = [{"vars": {"q": "a"}}]
    id_a = compute_item_id("ds", {"q": "a"})

    results, _ = _merge_hit_and_miss(
        full_dataset=full,
        dataset_name="ds",
        hits={id_a: "trc-A"},
        hit_scores={},  # 故意空
        miss_results=[],
    )
    assert results[0]["success"] is False
    assert results[0]["_cache_hit"] is True


def test_downgrade_scoreless_hits_to_miss() -> None:
    cache = CacheLookupResult(
        hits={"item-ok": "trc-ok", "item-missing": "trc-missing"},
        miss_item_ids=["item-new"],
        source_run_names=["run-1"],
    )

    result = _downgrade_scoreless_hits_to_miss(
        cache,
        {"trc-ok": 1.0},
        role="Baseline",
    )

    assert result.hits == {"item-ok": "trc-ok"}
    assert result.miss_item_ids == ["item-new", "item-missing"]
    assert result.source_run_names == ["run-1"]


def test_merge_treats_missing_miss_result_as_fail() -> None:
    """miss 跑失败导致结果缺失 → 保守判 fail，不抛。"""
    full = [{"vars": {"q": "a"}}]
    results, _ = _merge_hit_and_miss(
        full_dataset=full,
        dataset_name="ds",
        hits={},
        hit_scores={},
        miss_results=[],   # 应该有但空了
    )
    assert results[0]["success"] is False


def test_merge_preserves_full_dataset_order() -> None:
    full = [{"vars": {"q": str(i)}} for i in range(5)]
    miss_results = [{"vars": {"q": str(i)}, "success": True} for i in range(5)]

    results, _ = _merge_hit_and_miss(
        full_dataset=full,
        dataset_name="ds",
        hits={},
        hit_scores={},
        miss_results=miss_results,
    )
    assert [r["vars"]["q"] for r in results] == ["0", "1", "2", "3", "4"]


def test_merge_skips_non_dict_dataset_entries() -> None:
    """YAML 容错：null / 字符串等异常条目静默跳过。"""
    full = [{"vars": {"q": "a"}}, None, "garbage", {"vars": {"q": "b"}}]
    miss_results = [
        {"vars": {"q": "a"}, "success": True},
        {"vars": {"q": "b"}, "success": False},
    ]
    results, _ = _merge_hit_and_miss(
        full_dataset=full,
        dataset_name="ds",
        hits={},
        hit_scores={},
        miss_results=miss_results,
    )
    assert len(results) == 2


# ── _build_run_metadata ──


def _cache(hit_n: int = 0, miss_n: int = 0, sources: list[str] | None = None) -> CacheLookupResult:
    return CacheLookupResult(
        hits={f"item-{i}": f"trc-{i}" for i in range(hit_n)},
        miss_item_ids=[f"item-miss-{i}" for i in range(miss_n)],
        source_run_names=sources or [],
    )


def test_run_metadata_contains_required_fields() -> None:
    md = _build_run_metadata(
        role="baseline",
        prompt_name="intention-prompt",
        prompt_version=17,
        prompt_label="production",
        judge_model="qwen-max",
        stats={"total": 33, "pass": 28, "fail": 5, "rate": "84.8"},
        cache_result=_cache(hit_n=30, miss_n=3, sources=["src-1"]),
    )
    assert md["role"] == "baseline"
    assert md["prompt_version"] == 17
    assert md["pass_count"] == 28
    assert md["rate"] == 84.8
    assert md["cached_count"] == 30
    assert md["executed_count"] == 3
    assert md["cache_source_run_names"] == ["src-1"]


def test_run_metadata_merges_extra_fields() -> None:
    md = _build_run_metadata(
        role="candidate",
        prompt_name="p",
        prompt_version=2,
        prompt_label="staging",
        judge_model="m",
        stats={"total": 1, "pass": 1, "fail": 0, "rate": "100.0"},
        cache_result=_cache(),
        extra={"verdict": "A/B ✅", "regressions": 0, "improvements": 3},
    )
    assert md["verdict"] == "A/B ✅"
    assert md["regressions"] == 0
    assert md["improvements"] == 3
    assert md["role"] == "candidate"  # extra 不应覆盖基础字段


# ── _write_langfuse_run ──


class FakeLangfuse:
    """收集 submit_ingestion_batch + create_dataset_run_item 调用的轻量 mock。"""

    def __init__(self) -> None:
        self.ingestion_batches: list[list[dict]] = []
        self.run_items: list[dict] = []

    def submit_ingestion_batch(self, events: list[dict]) -> dict:
        self.ingestion_batches.append(events)
        return {"successes": [{"id": e["id"], "status": 201} for e in events], "errors": []}

    def create_dataset_run_item(
        self,
        *,
        run_name: str,
        dataset_item_id: str,
        trace_id: str,
        observation_id=None,
        run_description=None,
        metadata=None,
    ) -> dict:
        self.run_items.append({
            "run_name": run_name,
            "dataset_item_id": dataset_item_id,
            "trace_id": trace_id,
            "metadata": metadata,
            "run_description": run_description,
        })
        return {"id": f"ri-{len(self.run_items)}"}


def test_write_langfuse_run_only_first_item_carries_metadata() -> None:
    """4d.5 实测 run.metadata first-write-wins：除第一条外不带 metadata。"""
    fake = FakeLangfuse()
    full = _full_dataset_3()

    # 全 miss 场景
    _write_langfuse_run(
        client=fake,  # type: ignore[arg-type]
        dataset_name="ds",
        run_name="ab-baseline__p__v1__judge-m__t",
        full_dataset=full,
        cache_result=_cache(
            miss_n=0,
            sources=[],
        ),
        miss_results=[{"vars": {"q": "a"}, "success": True}, {"vars": {"q": "b"}, "success": False}, {"vars": {"q": "c"}, "success": True}],
        item_id_to_trace={},
        run_metadata={"role": "baseline", "prompt_name": "p", "prompt_version": 1, "rate": 66.7},
        role="baseline",
    )
    # 注意：上面 cache_result 的 miss_item_ids 是空的，所以 _write_langfuse_run
    # 不会为任何 case 生成 trace_id → 走不到 run-item 写入。换一个完整 miss 场景再测：


def test_write_langfuse_run_writes_traces_and_items_for_miss() -> None:
    fake = FakeLangfuse()
    full = _full_dataset_3()
    id_a = compute_item_id("ds", {"q": "a"})
    id_b = compute_item_id("ds", {"q": "b"})
    id_c = compute_item_id("ds", {"q": "c"})

    _write_langfuse_run(
        client=fake,  # type: ignore[arg-type]
        dataset_name="ds",
        run_name="ab-baseline__p__v1__judge-m__t",
        full_dataset=full,
        cache_result=CacheLookupResult(
            hits={}, miss_item_ids=[id_a, id_b, id_c], source_run_names=[]
        ),
        miss_results=[
            {"vars": {"q": "a"}, "success": True, "response": {"output": "ok-a"}},
            {"vars": {"q": "b"}, "success": False, "response": {"output": "ok-b"}},
            {"vars": {"q": "c"}, "success": True, "response": {"output": "ok-c"}},
        ],
        item_id_to_trace={},
        run_metadata={"role": "baseline", "prompt_name": "p", "prompt_version": 1, "rate": 66.7},
        role="baseline",
    )

    # 3 个 trace + 3 个 score 一批 ingestion
    assert len(fake.ingestion_batches) == 1
    events = fake.ingestion_batches[0]
    assert len(events) == 6
    types = [e["type"] for e in events]
    assert types.count("trace-create") == 3
    assert types.count("score-create") == 3

    # 3 个 run-items：第一个带 metadata，其余 None
    assert len(fake.run_items) == 3
    assert fake.run_items[0]["metadata"] is not None
    assert fake.run_items[0]["metadata"]["role"] == "baseline"
    assert fake.run_items[1]["metadata"] is None
    assert fake.run_items[2]["metadata"] is None


def test_write_langfuse_run_reuses_trace_id_for_hits_without_ingestion() -> None:
    """hit case 直接复用历史 trace_id，不发 ingestion，run-item 仍要写。"""
    fake = FakeLangfuse()
    full = _full_dataset_3()
    id_a = compute_item_id("ds", {"q": "a"})
    id_b = compute_item_id("ds", {"q": "b"})
    id_c = compute_item_id("ds", {"q": "c"})

    # a, b 命中；c miss
    item_id_to_trace = {id_a: "trc-OLD-A", id_b: "trc-OLD-B"}

    _write_langfuse_run(
        client=fake,  # type: ignore[arg-type]
        dataset_name="ds",
        run_name="ab-baseline__p__v1__judge-m__t",
        full_dataset=full,
        cache_result=CacheLookupResult(
            hits=item_id_to_trace.copy(),
            miss_item_ids=[id_c],
            source_run_names=["old-run"],
        ),
        miss_results=[
            {"vars": {"q": "c"}, "success": True, "response": {"output": "ok-c"}},
        ],
        item_id_to_trace=item_id_to_trace,
        run_metadata={"role": "baseline", "prompt_name": "p", "prompt_version": 1, "rate": 66.7},
        role="baseline",
    )

    # 只有 c miss → ingestion 2 events (trace + score)
    assert len(fake.ingestion_batches[0]) == 2

    # 3 个 run-items 都写了
    assert len(fake.run_items) == 3
    # hit 用历史 trace
    a_item = next(r for r in fake.run_items if r["dataset_item_id"] == id_a)
    b_item = next(r for r in fake.run_items if r["dataset_item_id"] == id_b)
    c_item = next(r for r in fake.run_items if r["dataset_item_id"] == id_c)
    assert a_item["trace_id"] == "trc-OLD-A"
    assert b_item["trace_id"] == "trc-OLD-B"
    # miss 用新生成的 UUID
    assert c_item["trace_id"] != "trc-OLD-A"
    assert c_item["trace_id"] != "trc-OLD-B"


def test_write_langfuse_run_no_ingestion_when_all_hits() -> None:
    """全部命中：不发 ingestion，但 run-items 仍然全写。"""
    fake = FakeLangfuse()
    full = _full_dataset_3()
    id_a = compute_item_id("ds", {"q": "a"})
    id_b = compute_item_id("ds", {"q": "b"})
    id_c = compute_item_id("ds", {"q": "c"})

    item_id_to_trace = {id_a: "trc-A", id_b: "trc-B", id_c: "trc-C"}
    _write_langfuse_run(
        client=fake,  # type: ignore[arg-type]
        dataset_name="ds",
        run_name="r",
        full_dataset=full,
        cache_result=CacheLookupResult(
            hits=item_id_to_trace.copy(), miss_item_ids=[], source_run_names=["src"]
        ),
        miss_results=[],
        item_id_to_trace=item_id_to_trace,
        run_metadata={"role": "baseline", "prompt_name": "p", "prompt_version": 1},
        role="baseline",
    )

    assert fake.ingestion_batches == []  # 全 hit → 不发 ingestion
    assert len(fake.run_items) == 3


def test_improvement_without_regression_is_marked_safe() -> None:
    baseline = [
        {"vars": {"query": "fixed"}, "success": False},
        {"vars": {"query": "same"}, "success": True},
    ]
    candidate = [
        {"vars": {"query": "fixed"}, "success": True},
        {"vars": {"query": "same"}, "success": True},
    ]
    base_stats = calc_stats(baseline)
    cand_stats = calc_stats(candidate)
    regressions, improvements = find_regressions_and_improvements(baseline, candidate)

    report = generate_ab_report(
        "agent",
        "production",
        "staging",
        base_stats,
        cand_stats,
        regressions,
        improvements,
        "base.json",
        "cand.json",
    )

    assert is_safe_to_upgrade(base_stats, cand_stats, regressions, improvements) is True
    assert "✅ **安全升级**" in report


def test_regression_blocks_safe_upgrade_even_with_net_improvement() -> None:
    baseline = [
        {"vars": {"query": "regressed"}, "success": True},
        {"vars": {"query": "fixed-1"}, "success": False},
        {"vars": {"query": "fixed-2"}, "success": False},
        {"vars": {"query": "fixed-3"}, "success": False},
    ]
    candidate = [
        {"vars": {"query": "regressed"}, "success": False},
        {"vars": {"query": "fixed-1"}, "success": True},
        {"vars": {"query": "fixed-2"}, "success": True},
        {"vars": {"query": "fixed-3"}, "success": True},
    ]
    base_stats = calc_stats(baseline)
    cand_stats = calc_stats(candidate)
    regressions, improvements = find_regressions_and_improvements(baseline, candidate)

    report = generate_ab_report(
        "agent",
        "production",
        "staging",
        base_stats,
        cand_stats,
        regressions,
        improvements,
        "base.json",
        "cand.json",
    )

    assert len(regressions) == 1
    assert len(improvements) == 3
    assert float(cand_stats["rate"]) > float(base_stats["rate"])
    assert is_safe_to_upgrade(base_stats, cand_stats, regressions, improvements) is False
    assert "✅ **安全升级**" not in report
    assert "⚠️ **存在回归**" in report


def test_improvement_within_tolerance_is_not_safe() -> None:
    """0 < rate_diff <= tolerance 时 verdict 为 SAME，报告不得写"安全升级"。"""
    baseline = [{"vars": {"query": f"q{i}"}, "success": i != 0} for i in range(100)]
    candidate = [{"vars": {"query": f"q{i}"}, "success": True} for i in range(100)]
    base_stats = calc_stats(baseline)
    cand_stats = calc_stats(candidate)
    regressions, improvements = find_regressions_and_improvements(baseline, candidate)

    assert len(regressions) == 0
    assert len(improvements) == 1  # 提升 1.0%，落在 tolerance=1.0 区间内

    report = generate_ab_report(
        "agent",
        "production",
        "staging",
        base_stats,
        cand_stats,
        regressions,
        improvements,
        "base.json",
        "cand.json",
        tolerance=1.0,
    )

    assert (
        is_safe_to_upgrade(
            base_stats, cand_stats, regressions, improvements, tolerance=1.0
        )
        is False
    )
    assert "✅ **安全升级**" not in report
    assert "➡️ **无明显改善**" in report


# ── 陈旧结果文件护栏（2026-07-28：Node ABI 崩溃 + 07-27 陈旧文件 → 假 A/B verdict）──

import click
import pytest

from eval_shared.cli import promptfoo_ab as _ab_module
from eval_shared.cli.promptfoo_ab import _run_promptfoo


def test_run_promptfoo_deletes_stale_output_before_running(tmp_path, monkeypatch) -> None:
    """PromptFoo 崩溃未产出新文件时必须报错，绝不能把上一轮的陈旧结果当本次结果。"""
    stale_output = tmp_path / "agent-ab-candidate.json"
    stale_output.write_text('{"results": {"results": []}}', encoding="utf-8")

    class _CrashResult:
        returncode = 1

    monkeypatch.setattr(_ab_module.subprocess, "run", lambda cmd: _CrashResult())

    with pytest.raises(click.ClickException, match="未生成结果文件"):
        _run_promptfoo(tmp_path / "cfg.yaml", str(stale_output), tmp_path / "p.yaml")

    assert not stale_output.exists(), "陈旧结果文件应在实跑前被清除"


def test_run_promptfoo_accepts_fresh_output_despite_nonzero_exit(tmp_path, monkeypatch) -> None:
    """telemetry 超时类非零退出码仍然容忍——但仅当本次真的写出了新文件。"""
    output = tmp_path / "agent-ab-candidate.json"

    class _TelemetryTimeout:
        returncode = 100

    def _fake_run(cmd):
        output.write_text('{"results": {"results": []}}', encoding="utf-8")
        return _TelemetryTimeout()

    monkeypatch.setattr(_ab_module.subprocess, "run", _fake_run)

    _run_promptfoo(tmp_path / "cfg.yaml", str(output), tmp_path / "p.yaml")
    assert output.exists()


def test_run_promptfoo_subset_rejects_incomplete_results(tmp_path, monkeypatch) -> None:
    """结果数 < 子集数 = PromptFoo 中途丢 case，必须硬报错而非静默混入统计（事故链第三道闸）。"""
    from eval_shared.cli.promptfoo_ab import _run_promptfoo_subset

    dataset = [{"vars": {"query": "a"}}, {"vars": {"query": "b"}}]
    monkeypatch.setattr(_ab_module, "_run_promptfoo", lambda *a, **k: None)
    monkeypatch.setattr(_ab_module, "load_results", lambda _: [{"vars": {"query": "a"}}])

    config = tmp_path / "promptfooconfig.yaml"
    config.write_text("prompts: [file://prompt.yaml]\n", encoding="utf-8")

    with pytest.raises(click.ClickException, match="结果不完整"):
        _run_promptfoo_subset(
            agent="agent",
            full_dataset=dataset,
            dataset_name="agent-golden",
            miss_item_ids=[
                _ab_module.compute_item_id("agent-golden", c["vars"]) for c in dataset
            ],
            business_config_path=config,
            prompt_path=tmp_path / "p.yaml",
            output_path=str(tmp_path / "out.json"),
            tmp_basename="test-subset-guard",
        )


# ── item id 锚点（契约 §2.3：存量 id 优先，与 sync push 同语义）──


def test_case_item_id_prefers_stored_id() -> None:
    from eval_shared.cli.promptfoo_ab import _case_item_id
    from eval_shared.common.dataset_item_id import compute_item_id

    stored = {"id": "intention-regression-abc123", "vars": {"query": "脱敏后文本"}}
    assert _case_item_id("intention-regression", stored) == "intention-regression-abc123"

    no_id = {"vars": {"query": "a"}}
    assert _case_item_id("intention-golden", no_id) == compute_item_id(
        "intention-golden", {"query": "a"}
    )
