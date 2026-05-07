"""
Langfuse REST API 客户端。

统一封装所有 Langfuse API 调用，CLI 和 DSPy 模块共用此客户端。
原 JS 版 7 个脚本各自实现了一套 fetch+auth，此处合并为一份。
"""

from __future__ import annotations

from typing import Any

import httpx

from eval_shared.common.config import get_langfuse_config


class LangfuseClient:
    """Langfuse REST API 客户端。"""

    def __init__(self, config: dict | None = None):
        if config is None:
            config = get_langfuse_config()
        self.base_url = config["base_url"]
        self.auth_header = config["auth_header"]
        self._client = httpx.Client(
            headers={"Authorization": f"Basic {self.auth_header}"},
            timeout=30.0,
        )

    def close(self) -> None:
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

    def get_dataset_items(self, name: str, limit: int = 100) -> list[dict]:
        """获取 dataset 的所有 items（单次最多 limit 条）。"""
        r = self._client.get(
            f"{self.base_url}/api/public/dataset-items",
            params={"datasetName": name, "limit": limit},
        )
        r.raise_for_status()
        return r.json().get("data", [])

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

    def get_scores(self, name: str, max_pages: int = 10) -> list[dict]:
        """分页拉取已有的 scores。"""
        all_scores: list[dict] = []
        page = 1

        while page <= max_pages:
            r = self._client.get(
                f"{self.base_url}/api/public/scores",
                params={"name": name, "limit": 100, "page": page},
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
