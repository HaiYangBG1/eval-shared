"""
Manual spike — 不是 pytest 测试，pytest 不会收集 spike_*.py 文件。

验证 Langfuse 的几个 spec 未明确写清楚的灰色行为，结果将写入 docs/dataset-run-migration.md：
  Q1. ingestion `body.id` 重复时是否真的 upsert（spec 说是，要验证）
  Q2. run.metadata 在多次 POST run-item 时是覆盖还是合并（spec 只说 "updates"）
  Q3. 重 POST 同 (runName, datasetItemId) 是新建还是更新 run-item
  Q4. ingestion 响应 207 + errors 字段格式

运行：
  cd /Users/zhouhaiyang/Documents/Obsidian\\ Vault/AI底座/eval-shared
  .venv/bin/python tests/spike_ingestion_dataset_run.py --env-file ../eval-ai-order/.env

会创建专属 dataset（前缀 `__spike_<ts>__`）和 traces，跑完 teardown 删除 run-items 和
 dataset items（dataset 容器保留——Langfuse 不支持 DELETE dataset，traces 保留——UUID 不冲突）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

from eval_shared.common.config import init_env
from eval_shared.common.langfuse_client import LangfuseClient


SPIKE_TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
DATASET_NAME = f"__spike_{SPIKE_TS}_dataset__"
RUN_NAME = f"__spike_{SPIKE_TS}_run__"


def _h(title: str) -> None:
    print(f"\n━━━━━━━━━━ {title} ━━━━━━━━━━", flush=True)


def _kv(label: str, value, max_len: int = 200) -> None:
    s = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    if len(s) > max_len:
        s = s[:max_len] + "..."
    print(f"  {label}: {s}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _retry_until(probe, *, label: str, attempts: int = 12, interval: float = 5.0):
    """轮询直到 probe() 返回真值（处理 Langfuse 异步索引延迟）。

    返回最后一次的 probe 结果（不论真假），加打印进度。
    """
    last = None
    for i in range(1, attempts + 1):
        last = probe()
        if last:
            print(f"    ✓ {label} ready after {i} attempts (~{i * interval:.0f}s)")
            return last
        if i < attempts:
            print(f"    ⏳ {label} not ready, retry {i}/{attempts}...", flush=True)
            time.sleep(interval)
    print(f"    ⚠️ {label} still not ready after {attempts} attempts (~{attempts * interval:.0f}s)")
    return last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="../eval-ai-order/.env")
    args = parser.parse_args()

    init_env(args.env_file)

    print(f"📍 Spike timestamp: {SPIKE_TS}")
    print(f"📍 Dataset:         {DATASET_NAME}")
    print(f"📍 Run name:        {RUN_NAME}")

    client = LangfuseClient()
    findings: dict[str, object] = {}
    item1_id: str | None = None
    item2_id: str | None = None

    try:
        # ── Setup ──
        _h("Setup: 创建测试 dataset + 2 items")
        ds = client.create_dataset(DATASET_NAME, description="Migration spike — auto-cleanup")
        _kv("dataset id", ds.get("id"))

        item1 = client.upsert_dataset_item({
            "datasetName": DATASET_NAME,
            "input": {"q": "spike-1"},
            "metadata": {"role": "spike"},
        })
        item1_id = item1.get("id")
        _kv("item1 id", item1_id)

        item2 = client.upsert_dataset_item({
            "datasetName": DATASET_NAME,
            "input": {"q": "spike-2"},
            "metadata": {"role": "spike"},
        })
        item2_id = item2.get("id")
        _kv("item2 id", item2_id)

        # ── Step 1: 通过 ingestion 创建 traces，故意复用 body.id ──
        _h("Step 1: POST /api/public/ingestion — 同 batch 含 2 条 body.id 相同的 trace")

        trace_a = str(uuid.uuid4())
        trace_b = str(uuid.uuid4())

        ingestion_body = {
            "batch": [
                # 同一个 trace_a，第一次 output=v1
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "type": "trace-create",
                    "body": {
                        "id": trace_a,
                        "timestamp": _now_iso(),
                        "name": "spike-trace-a",
                        "input": {"q": "v1-input"},
                        "output": "out-v1",
                        "metadata": {"variant": "first-write"},
                    },
                },
                # 同一个 trace_a，第二次 output=v2（验证 upsert）
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "type": "trace-create",
                    "body": {
                        "id": trace_a,
                        "timestamp": _now_iso(),
                        "name": "spike-trace-a",
                        "input": {"q": "v1-input"},
                        "output": "out-v2",
                        "metadata": {"variant": "second-write"},
                    },
                },
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "type": "trace-create",
                    "body": {
                        "id": trace_b,
                        "timestamp": _now_iso(),
                        "name": "spike-trace-b",
                        "input": {"q": "b-input"},
                        "output": "out-b",
                    },
                },
                # 故意发个错误事件：score 引用不存在的 trace
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "type": "score-create",
                    "body": {
                        "id": str(uuid.uuid4()),
                        "traceId": "definitely-non-existent-trace-id-xxx",
                        "name": "spike-test-score",
                        "value": 0.5,
                    },
                },
            ]
        }

        r = client._client.post(
            f"{client.base_url}/api/public/ingestion",
            json=ingestion_body,
        )
        _kv("ingestion HTTP status", r.status_code)
        result = r.json()
        successes = result.get("successes", [])
        errors = result.get("errors", [])
        _kv("response keys", list(result.keys()))
        _kv("successes count", len(successes))
        _kv("errors count", len(errors))
        if successes:
            _kv("first success", successes[0])
        if errors:
            _kv("first error", errors[0])

        findings["Q4_ingestion_response_shape"] = {
            "status": r.status_code,
            "top_keys": list(result.keys()),
            "success_count": len(successes),
            "error_count": len(errors),
            "first_error_keys": list(errors[0].keys()) if errors else None,
            "first_error_sample": errors[0] if errors else None,
        }

        # 等异步处理（轮询直到能 GET 到 trace）
        _h("Q1: 等待 trace_a 异步索引就绪（最多 60s）")

        def _probe_trace():
            try:
                r = client._client.get(f"{client.base_url}/api/public/traces/{trace_a}")
                if r.status_code == 200:
                    return r.json()
                return None
            except Exception:
                return None

        trace_data = _retry_until(_probe_trace, label="trace_a indexing")
        if trace_data:
            output_value = trace_data.get("output")
            metadata_value = trace_data.get("metadata")
            _kv("trace_a output", output_value)
            _kv("trace_a metadata", metadata_value)
            if output_value == "out-v2":
                findings["Q1_ingestion_upsert"] = "upsert (last write wins)"
            elif output_value == "out-v1":
                findings["Q1_ingestion_upsert"] = "first write wins (no upsert)"
            else:
                findings["Q1_ingestion_upsert"] = f"unexpected: {output_value!r}"
        else:
            findings["Q1_ingestion_upsert"] = "trace never indexed within 60s — async worker may be stalled"

        # ── Step 2: 创建第一个 run-item，带 metadata ──
        _h("Step 2: 第一次 POST run-item — metadata={a:1, role:first}")
        ri1 = client.create_dataset_run_item(
            run_name=RUN_NAME,
            dataset_item_id=item1_id,
            trace_id=trace_a,
            metadata={"a": 1, "role": "first"},
            run_description="spike first description",
        )
        _kv("ri1 id", ri1.get("id"))
        _kv("ri1 datasetRunId", ri1.get("datasetRunId"))

        # ── Step 3: 第二个 run-item，不同 datasetItemId，新 metadata ──
        _h("Step 3: 第二次 POST run-item — metadata={b:2, role:second}, description='spike second'")
        ri2 = client.create_dataset_run_item(
            run_name=RUN_NAME,
            dataset_item_id=item2_id,
            trace_id=trace_b,
            metadata={"b": 2, "role": "second"},
            run_description="spike second description",
        )
        _kv("ri2 id", ri2.get("id"))

        # ── Q2: run.metadata 是合并还是覆盖 ──
        _h("Q2: 等待 run-items 异步索引（前 2 条）")

        def _probe_2_items():
            try:
                rd = client.get_dataset_run(DATASET_NAME, RUN_NAME)
                items = rd.get("datasetRunItems") or []
                return rd if len(items) >= 2 else None
            except Exception:
                return None

        run_detail = _retry_until(_probe_2_items, label="run-items 2 indexed") or {}
        run_metadata = run_detail.get("metadata")
        run_description = run_detail.get("description")
        items_after_step3 = run_detail.get("datasetRunItems") or []
        _kv("run.metadata", run_metadata)
        _kv("run.description", run_description)
        _kv("items count", len(items_after_step3))

        if isinstance(run_metadata, dict):
            keys = set(run_metadata.keys())
            if {"a", "b", "role"} <= keys:
                findings["Q2_metadata_behavior"] = "merge (deep or shallow)"
            elif keys == {"b", "role"}:
                findings["Q2_metadata_behavior"] = "replace (last write wins)"
            elif keys == {"a", "role"}:
                findings["Q2_metadata_behavior"] = "first write wins (no update)"
            else:
                findings["Q2_metadata_behavior"] = f"other: {sorted(keys)}"
        else:
            findings["Q2_metadata_behavior"] = f"unexpected type: {type(run_metadata).__name__}"

        # ── Step 4: 重 POST 同 (runName, datasetItemId)，不同 traceId ──
        _h("Step 4: 第三次 POST — 同 datasetItemId=item1_id 但 traceId 改成 trace_b")
        ri3 = client.create_dataset_run_item(
            run_name=RUN_NAME,
            dataset_item_id=item1_id,  # 跟 ri1 同一个 datasetItemId
            trace_id=trace_b,           # 但 traceId 不同
        )
        _kv("ri3 id", ri3.get("id"))
        _kv("ri1 id (recap)", ri1.get("id"))
        _kv("ri3.id == ri1.id?", ri3.get("id") == ri1.get("id"))

        # ── Q3: 等待第三个 POST 索引就绪后 GET ──
        _h("Q3: 等待 run-items 全部就绪（含第三次 POST）")

        # 关键：是否新建可以由 ri3.id != ri1.id 提前判断；items_count 等待是为了 traceId 验证
        def _probe_3_items_or_stable():
            try:
                rd = client.get_dataset_run(DATASET_NAME, RUN_NAME)
                items = rd.get("datasetRunItems") or []
                # 如果新建：会有 3 条；如果更新：会保持 2 条但 traceId 变化。
                # 等到看见 ri3 出现（id 新出）或确认 stable
                if len(items) >= 3:
                    return rd
                # 看 ri3.id 是否在 items 列表里
                if any(it.get("id") == ri3.get("id") for it in items):
                    return rd
                return None
            except Exception:
                return None

        run_detail2 = _retry_until(_probe_3_items_or_stable, label="ri3 indexed") or {}
        items_final = run_detail2.get("datasetRunItems") or []
        _kv("items count", len(items_final))
        for it in items_final:
            print(
                f"    - id={(it.get('id') or '')[:8]}... "
                f"datasetItemId={(it.get('datasetItemId') or '')[:8]}... "
                f"traceId={(it.get('traceId') or '')[:8]}..."
            )

        same_item_count = sum(1 for it in items_final if it.get("datasetItemId") == item1_id)
        if same_item_count == 1:
            # 看 traceId 是 a 还是 b
            for it in items_final:
                if it.get("datasetItemId") == item1_id:
                    final_trace = it.get("traceId")
                    if final_trace == trace_b:
                        findings["Q3_run_item_dedup"] = "update in place (last write wins)"
                    elif final_trace == trace_a:
                        findings["Q3_run_item_dedup"] = "first write wins (no update)"
                    else:
                        findings["Q3_run_item_dedup"] = f"unexpected traceId: {final_trace}"
                    break
        elif same_item_count == 2:
            findings["Q3_run_item_dedup"] = "create new (allows duplicate (run, item))"
        else:
            findings["Q3_run_item_dedup"] = f"unexpected count: {same_item_count}"

    finally:
        # ── Teardown ──
        _h("Teardown")
        try:
            client.delete_dataset_run(DATASET_NAME, RUN_NAME)
            print(f"  ✓ deleted run: {RUN_NAME}")
        except Exception as e:
            print(f"  ⚠️ delete run failed: {e}")

        try:
            n = client.delete_all_dataset_items(DATASET_NAME)
            print(f"  ✓ deleted {n} dataset items")
        except Exception as e:
            print(f"  ⚠️ delete items failed: {e}")

        # dataset 容器保留（Langfuse 不支持 DELETE dataset），traces 保留（UUID 不冲突）
        client.close()

    # ── 最终发现汇总 ──
    _h("发现汇总")
    for q, v in findings.items():
        print(f"  {q}: {v}")
    print()
    return findings


if __name__ == "__main__":
    main()
