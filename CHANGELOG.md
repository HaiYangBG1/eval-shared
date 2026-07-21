# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

| 变更类型 | 版本号 | 示例 |
|----------|--------|------|
| 新增功能 | minor (`2.1.0`) | 新增 DSPy MIPROv2 优化 |
| 修复 bug | patch (`2.0.1`) | 修正 sync-dataset 分页 |
| 破坏性变更 | major (`3.0.0`) | 修改 CLI 参数接口 |

**原则**：只有 **≥ 2 个项目需要** 的规则才上推到本仓库。

---

## [2.1.1] - 2026-07-21

### Fixed — Bug 修复

- **`cli/promptfoo_ab.py`**：`is_safe_to_upgrade` 改为委托 `compute_verdict`（含 tolerance），`generate_ab_report` 接受 `tolerance` 参数。修复 `0 < rate_diff ≤ tolerance` 时报告写"✅ 安全升级"但 verdict / Langfuse label 为 🟰 的残留矛盾；同步修正"无净改善"过时文案。
- **`common/ab_verdict.py`**：docstring 对齐回归优先口径（此前仍写旧净改善描述）。
- **`pyproject.toml`**：版本号 bump 至 2.1.1（此前 CHANGELOG 已记但漏改）。
- **`AGENTS.md`**：升级判定策略行改为回归优先口径；CLI 清单补齐至 12 条（补 `eval-dataset-promote`、`eval-migrate-datasets-v2`）。

- **`cli/dspy_pipeline.py`**：agent 名推断优先使用 `output.prompt_name`，并在没有 prompt_name 时剥离 `{agent}-golden` / `{agent}-regression` / `{agent}-online-temp` 后缀。修复三层 dataset 架构下把 `intention-golden` 误当成本地目录，导致 A/B 阶段查找 `agents/intention-golden/promptfooconfig.yaml` 失败的问题。
- **`cli/promptfoo_ab.py` / `cli/dspy_pipeline.py`**：统一 A/B 决策口径为“有回归一律阻断”。修复 `verdict=A/B ❌` 但报告和流水线终端仍按旧净改善口径提示“安全升级”的矛盾。
- **`common/dataset_run_cache.py` / `cli/promptfoo_ab.py`**：缓存命中后 score 查询增加 traceId 回退；仍缺 score 的 cache hit 会降级为 miss 重新实跑，避免历史 score 未索引或复用 trace 时被当成失败，导致 A/B 通过率被大量低估。

### 测试

- 新增 `tests/test_dspy_pipeline.py` 覆盖 prompt_name 优先级、三层 dataset 后缀剥离和 legacy dataset 兜底。
- 新增回归优先门禁用例，覆盖“通过率提升但存在 PASS→FAIL 回归”时不得提示安全升级。
- 新增缓存 score 回退和 scoreless hit 降级测试。

---

## [2.1.0] - 2026-05-11

评估状态归档机制重构：从 Prompt label 滥用迁移到 Langfuse Dataset Run + 三态枚举。
详细演进过程、API 摸底、坑、决策记录见 [docs/dataset-run-migration.md](./docs/dataset-run-migration.md)。

### Added — 新增功能

- **三层 dataset 架构**：每个 agent 维护 `{agent}-golden`（基线）/ `{agent}-regression`（历史 bug 沉淀）/ `{agent}-online-temp`（eval-online 工作区，覆盖式）。
- **`common/ab_verdict.py`**：A/B 评估结果三态枚举 `ABVerdict.{BETTER, WORSE, SAME}`，对应 label `A/B ✅` / `A/B ❌` / `A/B 🟰`。导出 `compute_verdict(rate_diff, regressions, improvements, tolerance)` 和 `aggregate_verdicts(verdicts)`。
- **`common/dataset_run_cache.py`**：A/B 评估缓存查询。`lookup_cache()` 按 cache key `(prompt_name, prompt_version, judge_model, role)` 查历史 Dataset Run 中的 trace_id，命中复用避免重跑 LLM。`fetch_scores_by_trace_id()` 批量拉历史 score 给本地 stats 计算用。
- **`common/dataset_item_id.compute_item_id`**：sync_dataset 和 promptfoo_ab 共享的 item id 算法 `{dataset}-{sha1(sorted vars)[:10]}`，保证 push 与 cache 复算严格一致。
- **`common/promptfoo_subset.py`**：临时 dataset YAML + promptfooconfig 生成器，让 PromptFoo 只跑 miss 子集省 LLM 调用。
- **`common/ingestion.py`**：Langfuse ingestion 事件构造器 `build_trace_event` / `build_score_event` / `new_trace_id`。约定 score `name=promptfoo_pass`, `dataType=NUMERIC`, `value=1.0/0.0`（避开 BOOLEAN 写回 422 坑）。
- **`LangfuseClient` 新增 11 个方法**：
  - Dataset Run CRUD：`create_dataset_run_item` / `list_dataset_run_items` / `list_dataset_runs` / `get_dataset_run` / `delete_dataset_run`
  - Dataset Item 管理：`get_dataset_item` / `delete_dataset_item` / `delete_all_dataset_items`（覆盖式清空）
  - Ingestion：`submit_ingestion_batch`
  - Score v2 查询：`list_scores`（支持 `traceId` / `datasetRunId` 过滤）
- **新 CLI `eval-migrate-datasets-v2`**：把旧 `{agent}` dataset 一键迁移到三层架构，支持 `--all` / `--dry-run` / `--from-name`。
- **新 CLI `eval-dataset-promote`**：把 online-temp 里的某些 item promote 到 golden / regression，含 `--list` / `--dry-run`，metadata 写入 `promoted_from / promoted_at / promoted_reason` 审计字段。
- **`cli/sync_dataset.py` 加 `--type` 选项**：golden / regression / online-temp，默认 golden。默认 Langfuse dataset 名变为 `{agent}-{type}`，本地路径 `agents/{agent}/datasets/{type}.yaml`。
- **`cli/promptfoo_ab.py` 加 `--no-cache` / `--dataset` / `--tolerance` / `--sync-dataset` 选项**：
  - `--no-cache` 禁用缓存复用（CI 门禁推荐）
  - `--dataset` 覆盖默认 `{agent}-golden`，允许手工跑其他 dataset（如 regression）
  - `--tolerance` 容忍阈值（默认 1.0%），决定 SAME 状态边界
  - `--sync-dataset` 跑前从 Langfuse 拉最新 dataset 覆盖本地 YAML（默认关，CI 推荐开）
- **`cli/migrate_datasets_v2.py` 自动给重复 vars 加 `_variant`**：避免 `compute_item_id(vars)` 冲突合并丢数据。如 replenish dataset 中 7 对 vars 完全相同的 item 自动加 `_variant: 2/3/...`，确保 37/37 全部迁移。metadata 标记 `variant_auto_assigned: true` + 原始 input 备份。
- **`cli/eval_online.py` 加 `agent` 字段（evaluator 配置可选）**：填了启用 Dataset Run 写入到 `{agent}-online-temp`，并在跑前清空该 dataset；不填仍走旧 trace-level Score 路径（零破坏迁移）。
- **`promptfoo_ab` summary.json 新字段**：`verdict` / `tolerance` / `cache: {baseline_hits, baseline_miss, candidate_hits, candidate_miss}` / `langfuse_run_names`。

### Changed — 重构与一致性

- **`cli/dspy_pipeline._annotate_prompt` 签名变更**：从 `(prompt_name, ab_summary, dspy_report)` 改为 `(prompt_name, verdict: ABVerdict)`。DSPy 短路跳过 A/B 时也会标 🟰（之前漏标导致旧 label 残留）。
- **`cli/dspy_pipeline.py` 透传 `ab.tolerance` / `ab.dataset`**：从 `dspy-optimize.yaml` 读取并传给 `eval-promptfoo-ab` CLI。
- **`cli/promote_prompt.py` 门禁改枚举集合**：
  - 阻断条件 `lb == "A/B ❌"`（兼容旧 `startswith("A/B ❌")` 前缀格式）
  - promote 时**剥离全部 A/B 状态 label**（包括 `--force` 绕过失败时）——production 版本不再带评估状态噪音
  - 🟰 状态给警告但不阻断
- **`cli/promptfoo_ab.py` 主流程重写**：拉 prompt → 缓存查询 → miss-only 子集跑 → 合并 hit+miss → 写 Dataset Run；新增 `_run_promptfoo_subset` / `_merge_hit_and_miss` / `_write_langfuse_run` / `_build_run_metadata` helper。
- **`cli/sync_dataset.py` hash 算法抽出**：从 inline 改为 import `common/dataset_item_id.compute_item_id`，避免双份实现。

### BREAKING

- **`cli/sync_dataset.py` 默认 dataset 名变更**：`{agent}` → `{agent}-golden`。必须先跑 `eval-migrate-datasets-v2` 迁移 Langfuse 端数据，否则 pull/push 会 404。**业务项目迁移清单**见 [docs/dataset-run-migration.md](./docs/dataset-run-migration.md) §阶段 6d。
- **`cli/promptfoo_ab.py` 默认 dataset 名变更**：从 `{agent}` 改为 `{agent}-golden`，同上需要先迁移。

### Verified — Langfuse 异步行为（4d.5 spike）

实操验证了几个 spec 未明确写清的灰色行为：

- **ingestion `body.id` 重复 = first-write-wins**（spec 说是 upsert，实测不是）
- **`run.metadata` 多次 POST = first-write-wins**（必须第一个 run-item 一次性传完完整 metadata）
- **重 POST 同 `(runName, datasetItemId)` = update in place**（traceId 被新值替换）
- **GET 索引有 ~10-15s 异步延迟**（POST 后立即 GET 拿不到）
- **ingestion 业务错误异步吞掉**（HTTP 仍 207，需 GET 验证）

详见 [docs/dataset-run-migration.md](./docs/dataset-run-migration.md) §坑 #4 / #8 / #9。

### Deprecated — 待迁移

- **Langfuse `/api/public/ingestion` 端点**：被官方标记 deprecated，推荐迁移到 `/api/public/otel/v1/traces`。当前实现仍用 ingestion（兼容性好、简单），未来 Langfuse 真删除时需迁移到 OpenTelemetry。技术债记录在 migration log §坑 #7。

### 测试

- 14 → 108 测试用例（+94），全部 pass。新增模块的测试覆盖：ABVerdict / dataset_run_cache / dataset_item_id / promptfoo_subset / ingestion / dataset_promote / migrate_datasets_v2 / sync_dataset typed 路径。

---

## [2.0.1] - 2026-05-09

针对 `eval-shared` 与《AI Agent 评估体系》规范一轮系统性审查后的修复批次。详细的代码级 bug 记录见 [docs/BUGFIXES.md](./docs/BUGFIXES.md)。

### Fixed — Bug 修复

- **dspy/metrics.py**：`dir(example)` 改为 `example.inputs().keys()`，避免 DSPy 内部属性（`_completed`、`_demos` 等）泄漏到 LLM Judge 提示词。
- **dspy/uploader.py**：移除 `named_predictors()` 循环里的 `break`，多 predictor 模块（如 ChainOfThought）的 demos 现在能正确累积。
- **dspy/optimize.py**：以 `getattr(result, "score", result)` + `try/except` 替代脆弱的 `float() > 1` 解析，无法解析时回退 0.0 并告警。
- **cli/eval_online.py**：
  - JSONPath 拒绝 `[*]` / `?(` / `..` / `@.` 等不支持语法，给出清晰错误。
  - `BOOLEAN` 类型分数写回前转 `int(round(score))`，避免 Langfuse API 拒绝 float。
  - `get_scores` 传 `from_timestamp=since`，避免增量评估时全表扫描。
- **cli/promote_prompt.py**：用 `_RESERVED_LABELS` 白名单剥离 `staging`/`latest`/`production`，保留 A/B 评估标签；新增 `--force` 与 `A/B ❌` 阻断门，防止失败方案被误推上生产。
- **common/langfuse_client.py**：
  - `__init__` 支持注入 `http_client`，`_owns_client` 跟踪所有权；`close()` 仅关闭自有 client，避免共享 client 被意外关闭。
  - `get_scores` 新增 `from_timestamp` 参数。

### Changed — 重构与一致性

- **cli/promptfoo_ab.py**：移除 `_sync_prompt` 备份/恢复逻辑，改用 PromptFoo 的 `-p file://` 覆盖配置；流程从 4 步降到 3 步，不再触碰 `agents/<agent>/prompt.yaml`。
- **templates/dspy-optimize.example.yaml** → **dspy-optimize.template.yaml**：与其他模板命名（`*.template.yaml`）保持一致。
- **AI Agent 评估体系.md**：
  - §5.4 注明 `LANGFUSE_HOST` 与 `LANGFUSE_BASE_URL` 兼容，HOST 优先。
  - §6.1 移除已不支持的 `langfuse:intent-agent-prompt:staging` 引用，统一为 `file://`。
  - §5.5 npm scripts 列表补全 `test`、`export:dspy`、`promote`。
- **README.md**：模板目录清单同步新名称。

### Tests

- `tests/test_promote_prompt.py`：新增 `test_promote_blocks_when_ab_failed_label_present` 与 `test_promote_force_bypasses_ab_failure_gate`。
- `tests/test_langfuse_client.py`：`make_client` 改用 `http_client=` 注入，简化 fake。

验证：`pytest tests/ -v` → 8 passed。

---

## [2.0.0] - 2026 年初

将 eval-shared 从 Node.js/npm 包迁移为 Python 包。**CLI 命令名完全保持不变**。

### 业务项目变更

```diff
 # package.json — 移除 eval-shared npm 依赖
 "devDependencies": {
   "promptfoo": "^0.121.0",
-  "eval-shared": "^1.0.0"
 }
```

```bash
# 改用 pip 安装
pip install eval-shared
# 或
uv pip install eval-shared
```

**npm scripts 不需要改**：

```json
{
  "sync:dataset": "eval-sync-dataset",
  "sync:prompt": "eval-sync-prompt"
}
```

> 只要 eval-shared 的 CLI 在 PATH 中（pip install 后自动注册），npm scripts 照样能调用。

### 架构改进

| 维度 | v1 (JS) | v2 (Python) |
|------|---------|-------------|
| 语言 | Node.js | Python 3.11+ |
| 包管理 | npm | pip / uv |
| Langfuse 客户端 | 7 个脚本各自实现 | 统一 `langfuse_client.py` |
| DSPy 支持 | 仅导出 JSON | 加载 + 优化 + 上传全链路 |
| 依赖管理 | `package.json` | `pyproject.toml` |
