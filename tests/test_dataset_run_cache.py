from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eval_shared.common.dataset_run_cache import (
    CacheKey,
    build_run_name,
    build_run_name_prefix,
    lookup_cache,
)


# ── Run name encoding ──


def test_build_run_name_prefix_encodes_all_key_fields() -> None:
    key = CacheKey(
        prompt_name="intention-prompt",
        prompt_version=17,
        judge_model="qwen-max",
        role="baseline",
    )
    assert build_run_name_prefix(key) == (
        "ab-baseline__intention-prompt__v17__judge-qwen-max__"
    )


def test_build_run_name_prefix_sanitizes_special_chars() -> None:
    key = CacheKey(
        prompt_name="recommend/v2",        # `/` 会被替换
        prompt_version=3,
        judge_model="openai:gpt-4o",       # `:` 会被替换
        role="candidate",
    )
    assert build_run_name_prefix(key) == (
        "ab-candidate__recommend-v2__v3__judge-openai-gpt-4o__"
    )


def test_build_run_name_appends_timestamp() -> None:
    key = CacheKey("p", 1, "m")
    ts = datetime(2026, 5, 10, 14, 32, 0, tzinfo=timezone.utc)
    assert build_run_name(key, ts=ts) == "ab-baseline__p__v1__judge-m__20260510T143200Z"


# ── Cache lookup ──


class FakeClient:
    """最小 LangfuseClient mock：只关心 list_dataset_runs / get_dataset_run。"""

    def __init__(
        self,
        runs: list[dict],
        run_items: dict[str, list[dict]],
    ) -> None:
        self._runs = runs
        self._run_items = run_items
        self.run_detail_calls: list[str] = []

    def list_dataset_runs(self, _dataset_name: str, **_kwargs):
        return self._runs

    def get_dataset_run(self, _dataset_name: str, run_name: str):
        self.run_detail_calls.append(run_name)
        return {
            "name": run_name,
            "datasetRunItems": self._run_items.get(run_name, []),
        }


def _key(version: int = 17, role: str = "baseline") -> CacheKey:
    return CacheKey(
        prompt_name="intention-prompt",
        prompt_version=version,
        judge_model="qwen-max",
        role=role,  # type: ignore[arg-type]
    )


def test_lookup_cache_returns_empty_when_targets_empty() -> None:
    client = FakeClient(runs=[], run_items={})
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=[],
        cache_key=_key(),
    )
    assert result.hits == {}
    assert result.miss_item_ids == []
    assert result.source_run_names == []


def test_lookup_cache_all_miss_when_no_runs_match_prefix() -> None:
    client = FakeClient(
        runs=[{"name": "ab-baseline__OTHER__v17__judge-qwen-max__t1", "createdAt": "2026-05-09T00:00:00Z"}],
        run_items={},
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1", "item-2"],
        cache_key=_key(),
    )
    assert result.hit_count == 0
    assert result.miss_item_ids == ["item-1", "item-2"]
    assert client.run_detail_calls == []  # 前缀都不匹配，不应继续 GET run 详情


def test_lookup_cache_hits_from_single_matching_run() -> None:
    prefix = build_run_name_prefix(_key())
    run_name = prefix + "20260509T120000Z"
    client = FakeClient(
        runs=[{"name": run_name, "createdAt": "2026-05-09T12:00:00Z"}],
        run_items={
            run_name: [
                {"datasetItemId": "item-1", "traceId": "trc-A"},
                {"datasetItemId": "item-2", "traceId": "trc-B"},
                {"datasetItemId": "item-X", "traceId": "trc-X"},  # 不在 target 里，应忽略
            ]
        },
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1", "item-2", "item-3"],
        cache_key=_key(),
    )
    assert result.hits == {"item-1": "trc-A", "item-2": "trc-B"}
    assert result.miss_item_ids == ["item-3"]
    assert result.source_run_names == [run_name]


def test_lookup_cache_takes_newest_when_multiple_runs_have_same_item() -> None:
    """同一 item 出现在多个历史 run 时，取最新（createdAt 倒序）。"""
    prefix = build_run_name_prefix(_key())
    old_run = prefix + "20260501T120000Z"
    new_run = prefix + "20260509T120000Z"
    client = FakeClient(
        # 故意把旧的放前面，验证客户端会重新排序
        runs=[
            {"name": old_run, "createdAt": "2026-05-01T12:00:00Z"},
            {"name": new_run, "createdAt": "2026-05-09T12:00:00Z"},
        ],
        run_items={
            old_run: [{"datasetItemId": "item-1", "traceId": "trc-OLD"}],
            new_run: [{"datasetItemId": "item-1", "traceId": "trc-NEW"}],
        },
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1"],
        cache_key=_key(),
    )
    assert result.hits == {"item-1": "trc-NEW"}
    assert result.source_run_names == [new_run]


def test_lookup_cache_does_not_match_different_prompt_version() -> None:
    """v17 的 cache key 不应命中 v16 的历史 run。"""
    prefix_v16 = build_run_name_prefix(_key(version=16))
    v16_run = prefix_v16 + "20260509T120000Z"
    client = FakeClient(
        runs=[{"name": v16_run, "createdAt": "2026-05-09T12:00:00Z"}],
        run_items={v16_run: [{"datasetItemId": "item-1", "traceId": "trc-A"}]},
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1"],
        cache_key=_key(version=17),
    )
    assert result.hit_count == 0
    assert result.miss_item_ids == ["item-1"]


def test_lookup_cache_short_circuits_when_all_items_hit() -> None:
    """全部命中后不应继续 GET 后续 run（性能保证）。"""
    prefix = build_run_name_prefix(_key())
    first_run = prefix + "20260509T120000Z"
    second_run = prefix + "20260508T120000Z"
    client = FakeClient(
        runs=[
            {"name": first_run, "createdAt": "2026-05-09T12:00:00Z"},
            {"name": second_run, "createdAt": "2026-05-08T12:00:00Z"},
        ],
        run_items={
            first_run: [
                {"datasetItemId": "item-1", "traceId": "trc-A"},
                {"datasetItemId": "item-2", "traceId": "trc-B"},
            ],
            second_run: [{"datasetItemId": "item-1", "traceId": "trc-OLD"}],
        },
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1", "item-2"],
        cache_key=_key(),
    )
    assert result.hits == {"item-1": "trc-A", "item-2": "trc-B"}
    # 第一个 run 已命中全部 target，第二个 run 不应被请求
    assert client.run_detail_calls == [first_run]


# ── fetch_scores_by_trace_id ──


from eval_shared.common.dataset_run_cache import (
    PROMPTFOO_PASS_SCORE_NAME,
    fetch_scores_by_trace_id,
)


class FakeScoresClient(FakeClient):
    """扩展 FakeClient 支持 list_scores。"""

    def __init__(self, runs, run_items, scores_by_run_id):
        super().__init__(runs, run_items)
        # run-name → run-id 反查
        self._run_name_to_id = {r.get("name"): r.get("id") for r in runs}
        self._scores_by_run_id = scores_by_run_id

    def get_dataset_run(self, dataset_name: str, run_name: str):
        result = super().get_dataset_run(dataset_name, run_name)
        result["id"] = self._run_name_to_id.get(run_name)
        return result

    def list_scores(self, *, trace_id=None, dataset_run_id=None, name=None, **_kwargs):
        scores = self._scores_by_run_id.get(dataset_run_id, [])
        if name is not None:
            scores = [s for s in scores if s.get("name") == name]
        if trace_id is not None:
            scores = [s for s in scores if s.get("traceId") == trace_id]
        return list(scores)


def test_fetch_scores_indexes_by_trace_id() -> None:
    run_a = "ab-baseline__p__v17__judge-m__t1"
    run_b = "ab-baseline__p__v17__judge-m__t0"
    client = FakeScoresClient(
        runs=[
            {"name": run_a, "id": "rid-A"},
            {"name": run_b, "id": "rid-B"},
        ],
        run_items={},
        scores_by_run_id={
            "rid-A": [
                {"traceId": "trc-1", "value": 1.0, "name": PROMPTFOO_PASS_SCORE_NAME},
                {"traceId": "trc-2", "value": 0.0, "name": PROMPTFOO_PASS_SCORE_NAME},
                {"traceId": "trc-3", "value": 0.5, "name": "other-score"},  # 名字不对，被过滤
            ],
            "rid-B": [
                {"traceId": "trc-9", "value": 1.0, "name": PROMPTFOO_PASS_SCORE_NAME},
            ],
        },
    )

    result = fetch_scores_by_trace_id(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        source_run_names=[run_a, run_b],
    )

    assert result == {"trc-1": 1.0, "trc-2": 0.0, "trc-9": 1.0}


def test_fetch_scores_skips_runs_missing_id() -> None:
    """run 没有 id（被并发删等）时跳过，不抛异常。"""
    run_x = "ab-baseline__p__v17__judge-m__t1"
    client = FakeScoresClient(
        runs=[{"name": run_x, "id": None}],   # id 缺失
        run_items={},
        scores_by_run_id={},
    )

    result = fetch_scores_by_trace_id(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        source_run_names=[run_x],
    )

    assert result == {}


def test_fetch_scores_handles_run_load_failure() -> None:
    """单个 run 取不到不应阻断整体。"""
    run_ok = "ab-baseline__p__v17__judge-m__t1"
    run_bad = "ab-baseline__p__v17__judge-m__t0"

    class FlakyClient(FakeScoresClient):
        def get_dataset_run(self, dataset_name, run_name):
            if run_name == run_bad:
                raise RuntimeError("404")
            return super().get_dataset_run(dataset_name, run_name)

    client = FlakyClient(
        runs=[
            {"name": run_ok, "id": "rid-OK"},
            {"name": run_bad, "id": "rid-BAD"},
        ],
        run_items={},
        scores_by_run_id={
            "rid-OK": [{"traceId": "trc-1", "value": 1.0, "name": PROMPTFOO_PASS_SCORE_NAME}],
        },
    )

    result = fetch_scores_by_trace_id(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        source_run_names=[run_ok, run_bad],
    )

    assert result == {"trc-1": 1.0}


# ── 现有 lookup_cache 测试保留 ──


def test_lookup_cache_skips_runs_that_fail_to_load() -> None:
    """单个 run 取不到（被并发删除等）不影响其他 run 的命中。"""
    prefix = build_run_name_prefix(_key())
    bad_run = prefix + "20260509T120000Z"
    good_run = prefix + "20260508T120000Z"

    class FlakyClient(FakeClient):
        def get_dataset_run(self, dataset_name: str, run_name: str):
            if run_name == bad_run:
                raise RuntimeError("404 deleted")
            return super().get_dataset_run(dataset_name, run_name)

    client = FlakyClient(
        runs=[
            {"name": bad_run, "createdAt": "2026-05-09T12:00:00Z"},
            {"name": good_run, "createdAt": "2026-05-08T12:00:00Z"},
        ],
        run_items={
            good_run: [{"datasetItemId": "item-1", "traceId": "trc-OK"}],
        },
    )
    result = lookup_cache(
        client,  # type: ignore[arg-type]
        dataset_name="intention-golden",
        target_item_ids=["item-1"],
        cache_key=_key(),
    )
    assert result.hits == {"item-1": "trc-OK"}
    assert result.source_run_names == [good_run]
