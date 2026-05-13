from __future__ import annotations

from eval_shared.common.langfuse_client import LangfuseClient


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, pages: dict[int, list[dict]]):
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, _url: str, params: dict) -> FakeResponse:
        self.calls.append(params)
        page = params["page"]
        return FakeResponse({
            "data": self.pages.get(page, []),
            "meta": {"totalPages": len(self.pages)},
        })


def make_client(fake_http: FakeHttpClient) -> LangfuseClient:
    return LangfuseClient(
        config={"base_url": "https://langfuse.example", "auth_header": "token"},
        http_client=fake_http,  # type: ignore[arg-type]
    )


def test_get_dataset_items_paginates_until_all_pages_are_loaded() -> None:
    fake_http = FakeHttpClient({
        1: [{"id": "a"}],
        2: [{"id": "b"}],
        3: [{"id": "c"}],
    })
    client = make_client(fake_http)

    assert client.get_dataset_items("agent") == [
        {"id": "a"},
        {"id": "b"},
        {"id": "c"},
    ]
    assert [call["page"] for call in fake_http.calls] == [1, 2, 3]


def test_get_dataset_items_respects_limit_across_pages() -> None:
    fake_http = FakeHttpClient({
        1: [{"id": "a"}, {"id": "b"}],
        2: [{"id": "c"}, {"id": "d"}],
    })
    client = make_client(fake_http)

    assert client.get_dataset_items("agent", limit=3, page_size=2) == [
        {"id": "a"},
        {"id": "b"},
        {"id": "c"},
    ]
    assert [call["limit"] for call in fake_http.calls] == [2, 1]


# ── Dataset Run methods ──


class FakeMultiMethodHttp:
    """支持 GET/POST/DELETE 的 mock，用于测试新增的 dataset-run 相关方法。"""

    def __init__(self) -> None:
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.get_responses: dict[str, list[dict]] = {}  # url → list of payloads (consumed in order)
        self.post_response: dict = {"id": "ok"}
        self.delete_response: dict = {"message": "deleted"}

    def get(self, url: str, params: dict | None = None) -> FakeResponse:
        self.gets.append((url, params or {}))
        queue = self.get_responses.get(url, [{"data": [], "meta": {}}])
        payload = queue.pop(0) if queue else {"data": [], "meta": {}}
        return FakeResponse(payload)

    def post(self, url: str, json: dict) -> FakeResponse:
        self.posts.append((url, json))
        return FakeResponse(self.post_response)

    def delete(self, url: str) -> FakeResponse:
        self.deletes.append(url)
        return FakeResponse(self.delete_response)


def make_multi_client(fake: FakeMultiMethodHttp) -> LangfuseClient:
    return LangfuseClient(
        config={"base_url": "https://langfuse.example", "auth_header": "token"},
        http_client=fake,  # type: ignore[arg-type]
    )


def test_create_dataset_run_item_builds_minimum_body() -> None:
    fake = FakeMultiMethodHttp()
    client = make_multi_client(fake)

    client.create_dataset_run_item(
        run_name="ab-baseline__intention__v17",
        dataset_item_id="item-1",
        trace_id="trc-abc",
    )

    assert len(fake.posts) == 1
    url, body = fake.posts[0]
    assert url == "https://langfuse.example/api/public/dataset-run-items"
    assert body == {
        "runName": "ab-baseline__intention__v17",
        "datasetItemId": "item-1",
        "traceId": "trc-abc",
    }


def test_create_dataset_run_item_includes_optional_fields_when_set() -> None:
    fake = FakeMultiMethodHttp()
    client = make_multi_client(fake)

    client.create_dataset_run_item(
        run_name="r1",
        dataset_item_id="item-1",
        trace_id="trc-abc",
        observation_id="obs-1",
        run_description="A/B baseline for prompt v17",
        metadata={"prompt_version": 17, "role": "baseline"},
    )

    _, body = fake.posts[0]
    assert body["observationId"] == "obs-1"
    assert body["runDescription"] == "A/B baseline for prompt v17"
    assert body["metadata"] == {"prompt_version": 17, "role": "baseline"}


def test_list_dataset_run_items_paginates() -> None:
    fake = FakeMultiMethodHttp()
    url = "https://langfuse.example/api/public/dataset-run-items"
    fake.get_responses[url] = [
        {"data": [{"id": "ri-1"}, {"id": "ri-2"}], "meta": {}},
        {"data": [{"id": "ri-3"}], "meta": {}},  # 短页 → 终止
    ]
    client = make_multi_client(fake)

    items = client.list_dataset_run_items(
        dataset_id="ds-1", run_name="r1", page_size=2
    )

    assert [i["id"] for i in items] == ["ri-1", "ri-2", "ri-3"]
    assert [call[1]["page"] for call in fake.gets] == [1, 2]
    assert all(call[1]["datasetId"] == "ds-1" for call in fake.gets)
    assert all(call[1]["runName"] == "r1" for call in fake.gets)


def test_list_dataset_runs_paginates() -> None:
    fake = FakeMultiMethodHttp()
    url = "https://langfuse.example/api/public/datasets/intention-golden/runs"
    fake.get_responses[url] = [
        {"data": [{"name": "r1"}, {"name": "r2"}], "meta": {}},
        {"data": [], "meta": {}},
    ]
    client = make_multi_client(fake)

    runs = client.list_dataset_runs("intention-golden", page_size=2)
    assert [r["name"] for r in runs] == ["r1", "r2"]


def test_delete_dataset_run_calls_delete_endpoint() -> None:
    fake = FakeMultiMethodHttp()
    client = make_multi_client(fake)

    client.delete_dataset_run("intention-golden", "r1")

    assert fake.deletes == [
        "https://langfuse.example/api/public/datasets/intention-golden/runs/r1"
    ]


def test_submit_ingestion_batch_posts_events_under_batch_key() -> None:
    fake = FakeMultiMethodHttp()
    fake.post_response = {"successes": [{"id": "e1", "status": 201}], "errors": []}
    client = make_multi_client(fake)

    events = [
        {"id": "e1", "timestamp": "2026-05-10T00:00:00.000Z", "type": "trace-create", "body": {"id": "t1"}},
    ]
    result = client.submit_ingestion_batch(events)

    assert len(fake.posts) == 1
    url, body = fake.posts[0]
    assert url == "https://langfuse.example/api/public/ingestion"
    assert body == {"batch": events}
    assert result == {"successes": [{"id": "e1", "status": 201}], "errors": []}


def test_list_scores_filters_by_trace_id() -> None:
    fake = FakeMultiMethodHttp()
    url = "https://langfuse.example/api/public/v2/scores"
    fake.get_responses[url] = [{"data": [{"id": "s1", "value": 1.0}], "meta": {}}]
    client = make_multi_client(fake)

    scores = client.list_scores(trace_id="trc-1", name="promptfoo_pass")

    assert scores == [{"id": "s1", "value": 1.0}]
    assert fake.gets[0][1]["traceId"] == "trc-1"
    assert fake.gets[0][1]["name"] == "promptfoo_pass"


def test_list_scores_filters_by_dataset_run_id() -> None:
    fake = FakeMultiMethodHttp()
    url = "https://langfuse.example/api/public/v2/scores"
    fake.get_responses[url] = [
        {"data": [{"id": "s1"}, {"id": "s2"}], "meta": {}},
        {"data": [], "meta": {}},
    ]
    client = make_multi_client(fake)

    scores = client.list_scores(dataset_run_id="run-1", page_size=2)

    assert [s["id"] for s in scores] == ["s1", "s2"]
    assert fake.gets[0][1]["datasetRunId"] == "run-1"


def test_delete_all_dataset_items_iterates_each_item() -> None:
    fake = FakeMultiMethodHttp()
    list_url = "https://langfuse.example/api/public/dataset-items"
    fake.get_responses[list_url] = [
        {"data": [{"id": "item-1"}, {"id": "item-2"}, {"id": "item-3"}], "meta": {}},
        {"data": [], "meta": {}},
    ]
    client = make_multi_client(fake)

    deleted = client.delete_all_dataset_items("intention-online-temp")

    assert deleted == 3
    assert fake.deletes == [
        "https://langfuse.example/api/public/dataset-items/item-1",
        "https://langfuse.example/api/public/dataset-items/item-2",
        "https://langfuse.example/api/public/dataset-items/item-3",
    ]
