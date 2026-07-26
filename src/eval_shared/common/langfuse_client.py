"""
Langfuse REST API 客户端。

统一封装所有 Langfuse API 调用，CLI 和 DSPy 模块共用此客户端。
原 JS 版 7 个脚本各自实现了一套 fetch+auth，此处合并为一份。
"""

from __future__ import annotations

import httpx

from eval_shared.common.config import get_langfuse_config


class LangfuseClient:
    """Langfuse REST API 客户端。"""

    def __init__(
        self,
        config: dict | None = None,
        http_client: httpx.Client | None = None,
    ):
        if config is None:
            config = get_langfuse_config()
        self.base_url = config["base_url"]
        self.auth_header = config["auth_header"]
        if http_client is not None:
            # 测试或外部复用场景下注入自定义 client，由调用方负责生命周期
            self._client = http_client
            self._owns_client = False
        else:
            # ssl_verify=False 用于自签/IP 证书的私有部署；http→https 强制跳转
            # 不能靠 follow_redirects 解决（302 会把 POST 降级为 GET），
            # base_url 必须直接写 https。
            self._client = httpx.Client(
                headers={"Authorization": f"Basic {self.auth_header}"},
                timeout=30.0,
                verify=config.get("ssl_verify", True),
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Dataset ──

    def get_dataset(self, name: str) -> dict:
        """获取 dataset 元信息。"""
        r = self._client.get(
            f"{self.base_url}/api/public/v2/datasets/{name}"
        )
        r.raise_for_status()
        return r.json()

    def get_dataset_items(
        self,
        name: str,
        limit: int | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """分页获取 dataset items；limit=None 表示拉取全部。"""
        all_items: list[dict] = []
        page = 1

        while limit is None or len(all_items) < limit:
            actual_page_size = page_size
            if limit is not None:
                actual_page_size = min(page_size, limit - len(all_items))
            if actual_page_size <= 0:
                break

            r = self._client.get(
                f"{self.base_url}/api/public/dataset-items",
                params={
                    "datasetName": name,
                    "limit": actual_page_size,
                    "page": page,
                },
            )
            r.raise_for_status()
            payload = r.json()
            items = payload.get("data", [])
            all_items.extend(items)

            meta = payload.get("meta") or {}
            total_pages = meta.get("totalPages") or meta.get("total_pages")
            if not items:
                break
            if total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(items) < actual_page_size:
                break
            page += 1

        return all_items if limit is None else all_items[:limit]

    def create_dataset(self, name: str, description: str = "") -> dict:
        """创建新的 dataset。"""
        r = self._client.post(
            f"{self.base_url}/api/public/v2/datasets",
            json={"name": name, "description": description},
        )
        r.raise_for_status()
        return r.json()

    def upsert_dataset_item(self, body: dict) -> dict:
        """创建或更新 dataset item（按 id 幂等 upsert）。"""
        r = self._client.post(
            f"{self.base_url}/api/public/dataset-items",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def dataset_exists(self, name: str) -> bool:
        """检查 dataset 是否存在。"""
        r = self._client.get(
            f"{self.base_url}/api/public/v2/datasets/{name}"
        )
        return r.status_code == 200

    def get_dataset_item(self, item_id: str) -> dict:
        """按 id 拉单条 dataset item（用于 eval-dataset-promote 跨 dataset 复制）。"""
        r = self._client.get(
            f"{self.base_url}/api/public/dataset-items/{item_id}"
        )
        r.raise_for_status()
        return r.json()

    def delete_dataset_item(self, item_id: str) -> dict:
        """删除单条 dataset item（同时级联删除该 item 的所有 run-items，不可逆）。"""
        r = self._client.delete(
            f"{self.base_url}/api/public/dataset-items/{item_id}"
        )
        r.raise_for_status()
        return r.json()

    def delete_all_dataset_items(self, dataset_name: str) -> int:
        """清空某 dataset 下所有 items（用于 online-temp 跑前覆盖式清空）。

        ⚠️ Langfuse 没有批量清空 API，只能逐条 DELETE。N 条 item = N 次 API 调用。
        线上量大时考虑改并发，当前实现按串行处理。
        """
        items = self.get_dataset_items(dataset_name)
        deleted = 0
        for item in items:
            item_id = item.get("id")
            if item_id:
                self.delete_dataset_item(item_id)
                deleted += 1
        return deleted

    # ── Dataset Runs ──
    # 关键事实（来自 Langfuse OpenAPI spec, 2026-05）：
    # 1. 没有 POST /dataset-runs 端点；run 通过 POST /dataset-run-items 隐式创建
    # 2. metadata 字段挂在 run 级（不是 item 级），run-item 自身没有 metadata
    # 3. GET /dataset-run-items 仅支持按 datasetId + runName 过滤，不支持 metadata 过滤
    # 4. metadata 在 POST item 时携带会更新到 run；覆盖 vs 合并行为待实测

    def create_dataset_run_item(
        self,
        *,
        run_name: str,
        dataset_item_id: str,
        trace_id: str,
        observation_id: str | None = None,
        run_description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """创建 dataset run item；run 不存在时由 Langfuse 隐式创建。

        Args:
            run_name: Run 名称；不存在时隐式创建。我们的命名规范：
                例如 "ab-baseline__intention-prompt__v17__judge-qwen-max__20260510T120000Z"
            dataset_item_id: 关联的 dataset item id（必填）
            trace_id: 关联的 Langfuse trace id（必填，trace 必须已存在）
            observation_id: 可选；指向 trace 内具体 observation
            run_description: 可选；首次创建 run 时设置 description
            metadata: 可选；写到 **run 级别**（非 item 级别）。
                ⚠️ 重 POST 时携带 metadata 会更新 run，建议仅第一个 item 携带，避免反复触发更新。
        """
        body: dict[str, object] = {
            "runName": run_name,
            "datasetItemId": dataset_item_id,
            "traceId": trace_id,
        }
        if observation_id is not None:
            body["observationId"] = observation_id
        if run_description is not None:
            body["runDescription"] = run_description
        if metadata is not None:
            body["metadata"] = metadata
        r = self._client.post(
            f"{self.base_url}/api/public/dataset-run-items",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def list_dataset_run_items(
        self,
        *,
        dataset_id: str,
        run_name: str,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict]:
        """分页拉取某 run 下的 run-items（用于缓存命中查询）。"""
        all_items: list[dict] = []
        page = 1
        while page <= max_pages:
            r = self._client.get(
                f"{self.base_url}/api/public/dataset-run-items",
                params={
                    "datasetId": dataset_id,
                    "runName": run_name,
                    "limit": page_size,
                    "page": page,
                },
            )
            r.raise_for_status()
            payload = r.json()
            items = payload.get("data", [])
            all_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_items

    def list_dataset_runs(
        self,
        dataset_name: str,
        *,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> list[dict]:
        """列出 dataset 下所有 runs（按 createdAt 倒序，由 Langfuse 服务端决定）。

        缓存查询用：取回所有 run 后客户端按 runName 前缀过滤命中候选。
        """
        all_runs: list[dict] = []
        page = 1
        while page <= max_pages:
            r = self._client.get(
                f"{self.base_url}/api/public/datasets/{dataset_name}/runs",
                params={"limit": page_size, "page": page},
            )
            r.raise_for_status()
            payload = r.json()
            runs = payload.get("data", [])
            all_runs.extend(runs)
            if len(runs) < page_size:
                break
            page += 1
        return all_runs

    def get_dataset_run(self, dataset_name: str, run_name: str) -> dict:
        """获取单个 run 含全部 run-items（DatasetRunWithItems）。"""
        r = self._client.get(
            f"{self.base_url}/api/public/datasets/{dataset_name}/runs/{run_name}"
        )
        r.raise_for_status()
        return r.json()

    def delete_dataset_run(self, dataset_name: str, run_name: str) -> dict:
        """删除 run 及其所有 run-items（不可逆）。"""
        r = self._client.delete(
            f"{self.base_url}/api/public/datasets/{dataset_name}/runs/{run_name}"
        )
        r.raise_for_status()
        return r.json()

    # ── Prompt ──

    def get_prompt(self, name: str, label: str | None = None) -> dict:
        """获取 prompt（可选指定 label）。"""
        params = {}
        if label:
            params["label"] = label
        r = self._client.get(
            f"{self.base_url}/api/public/v2/prompts/{name}",
            params=params,
        )
        r.raise_for_status()
        return r.json()

    def create_prompt(self, body: dict) -> dict:
        """创建新版本的 prompt。"""
        r = self._client.post(
            f"{self.base_url}/api/public/v2/prompts",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def list_prompt_meta(self, name: str) -> dict:
        """列出 prompt 元信息（含 versions 版本号列表）。

        GET /api/public/v2/prompts?name= 返回 PromptMeta；找不到返回空 dict。
        """
        r = self._client.get(
            f"{self.base_url}/api/public/v2/prompts",
            params={"name": name},
        )
        r.raise_for_status()
        for item in r.json().get("data", []):
            if item.get("name") == name:
                return item
        return {}

    def update_prompt_labels(self, name: str, version: int, labels: list[str]) -> dict:
        """更新指定 prompt 版本的 labels。

        使用 PATCH /api/public/v2/prompts/{name}/versions/{version}
        Labels 在所有版本中唯一（同一 label 只能属于一个版本）。
        ⚠️ `newLabels` 语义是**只增/移动、不删除**（2026-07-24 实测）：
        把 label 加到本版本会自动从其它版本上移走，但无法从本版本上删除。
        """
        r = self._client.patch(
            f"{self.base_url}/api/public/v2/prompts/{name}/versions/{version}",
            json={"newLabels": labels},
        )
        r.raise_for_status()
        return r.json()

    # ── Observations ──

    def get_observations(
        self,
        name: str,
        since: str,
        limit: int = 50,
        page_size: int = 100,
    ) -> list[dict]:
        """分页拉取 GENERATION 类型的 observations。"""
        all_obs: list[dict] = []
        page = 1
        actual_page_size = min(limit, page_size)

        while len(all_obs) < limit:
            r = self._client.get(
                f"{self.base_url}/api/public/observations",
                params={
                    "type": "GENERATION",
                    "name": name,
                    "limit": actual_page_size,
                    "page": page,
                    "fromTimestamp": since,
                },
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data", [])
            all_obs.extend(items)
            if len(items) < actual_page_size:
                break
            page += 1

        return all_obs[:limit]

    # ── Scores ──

    def get_scores(
        self,
        name: str,
        max_pages: int = 10,
        from_timestamp: str | None = None,
    ) -> list[dict]:
        """分页拉取已有的 scores。

        Args:
            name: Score 名称
            max_pages: 最大翻页数（默认 10 页 = 1000 条）
            from_timestamp: ISO 时间戳，仅拉取此时间之后的 score。
                            建议设置为评估窗口起点，避免在历史 score 量大时全表扫描。
        """
        all_scores: list[dict] = []
        page = 1

        while page <= max_pages:
            params: dict[str, object] = {"name": name, "limit": 100, "page": page}
            if from_timestamp:
                params["fromTimestamp"] = from_timestamp
            r = self._client.get(
                f"{self.base_url}/api/public/scores",
                params=params,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data", [])
            all_scores.extend(items)
            if len(items) < 100:
                break
            page += 1

        return all_scores

    def write_score(self, body: dict) -> dict:
        """写入评估分数。"""
        r = self._client.post(
            f"{self.base_url}/api/public/scores",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def submit_ingestion_batch(self, events: list[dict]) -> dict:
        """POST 一批 ingestion 事件（trace-create / score-create / ...）。

        ⚠️ Langfuse 异步处理：4d.5 spike 实测发现
            - body.id 重复时 first-write-wins（不是 spec 说的 upsert）
            - 业务错误（如引用不存在的 traceId）不在响应里报，HTTP 仍返回 207
            - 数据写入到可被 GET 有 ~10-15 秒索引延迟
        ⚠️ 单批上限 3.5 MB（spec 明确）。当前实现不分批，由调用方控制（实际场景如 33 条
            trace 远小于上限，不需要分批；将来 eval-online 大批量时再补分批逻辑）。
        ⚠️ 端点已被官方标记 deprecated，推荐 /api/public/otel/v1/traces。
            技术债：未来 Langfuse 真删除时需迁移到 OpenTelemetry。
        """
        r = self._client.post(
            f"{self.base_url}/api/public/ingestion",
            json={"batch": events},
        )
        r.raise_for_status()
        return r.json()

    def list_scores(
        self,
        *,
        trace_id: str | None = None,
        dataset_run_id: str | None = None,
        name: str | None = None,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> list[dict]:
        """v2 scores 列表查询，支持按 trace_id / datasetRunId / name 过滤。

        - 按 datasetRunId 拉一个历史 run 的全部 scores 是 cache 命中复用的高效路径
        - 按 traceId 单条精确查询适合验证某条 trace 是否已写入 score
        - 仅 trace_id+name 是 (traceId, name) 维度，能定位具体 score 实例
        """
        all_scores: list[dict] = []
        page = 1
        while page <= max_pages:
            params: dict[str, object] = {"limit": page_size, "page": page}
            if trace_id is not None:
                params["traceId"] = trace_id
            if dataset_run_id is not None:
                params["datasetRunId"] = dataset_run_id
            if name is not None:
                params["name"] = name
            r = self._client.get(
                f"{self.base_url}/api/public/v2/scores",
                params=params,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data", [])
            all_scores.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_scores
