# 评估归档机制重构：从 prompt label 迁移到 Dataset Run

> 工作日志。每次改动的大纲、问题、坑都追加到这里。
> 完成后归档到 `BUGFIXES.md` 或 `CHANGELOG.md`。

## 背景与目标

当前评估状态归档存在两个问题：
1. PromptFoo A/B 跑完后把 `A/B ✅ 67.7%→80.6%` 写入 Langfuse Prompt 的 `label`，**滥用了 label 的"版本别名"语义**——promote 时跟随版本上生产、每次评估覆盖丢历史、无法挂结构化数据。
2. eval-online 只写 trace-level Score，没有 run 级聚合视图。

**目标设计**（已与用户敲定）：
- **Dataset Run** 当数据库：A/B 和 eval-online 都写 Dataset Run，承载结构化明细 + 历史趋势（"数据可视化"）
- **prompt label** 当仪表盘：固定 3 态枚举 `A/B ✅ / A/B ❌ / A/B 🟰`，promote 时统一剥离（"结果可视化"）
- **缓存**：cache key = `(prompt_name, prompt_version, dataset_item_id, judge_model)`；命中复用历史 score，未命中**必须实跑**（不允许偷懒）

完整设计见会话 §"三态最终方案" / §"Dataset Run 字段" / §"缓存策略"。

---

## 阶段 1：Langfuse Dataset Run REST API 摸底

**时间**：2026-05-10
**目的**：在动 LangfuseClient 之前，确认 API 能力边界

### API 能力清单（从官方 OpenAPI spec 提取）

来源：`langfuse/langfuse:fern/apis/server/definition/{dataset-run-items,commons}.yml`（main 分支）

| 端点 | 方法 | 用途 | 备注 |
|------|------|------|------|
| `/api/public/dataset-run-items` | POST | 创建 run item | run 不存在时**隐式创建** |
| `/api/public/dataset-run-items` | GET | 列 run items | query: `datasetId` + `runName` + page/limit |
| `/api/public/datasets/{datasetName}/runs` | GET | 列 runs | query: page/limit |
| `/api/public/datasets/{datasetName}/runs/{runName}` | GET | 获取单 run（含 items） | 返回 `DatasetRunWithItems` |
| `/api/public/datasets/{datasetName}/runs/{runName}` | DELETE | 删除 run（含所有 items） | — |

**`POST /dataset-run-items` 请求体**：

```yaml
runName: string             # 必填；不存在则隐式创建 run
runDescription: optional<string>   # 创建/更新 run 的 description
metadata: optional<unknown>        # 创建/更新 run 的 metadata（不是 item 的）
datasetItemId: string       # 必填
observationId: optional<string>
traceId: optional<string>   # 强烈建议提供（旧 SDK 才从 observationId 反推）
datasetVersion: optional<datetime>
createdAt: optional<datetime>
```

**类型 `DatasetRun`**：`id / name / description / metadata(unknown) / datasetId / datasetName / createdAt / updatedAt`
**类型 `DatasetRunItem`**：`id / datasetRunId / datasetRunName / datasetItemId / traceId / observationId / createdAt / updatedAt`

### 🚨 摸底发现的 5 个坑

#### 坑 #1：Run 无法以 0 items 创建
没有独立的 `POST /dataset-runs` 端点，run 通过 `POST /dataset-run-items` 隐式创建。**意味着每个 run 至少要写一个 item**。

**影响**：`eval-online` 的"统计快照"思路不能简单地"创建 run + 只挂 metadata"——必须有 item。

#### 坑 #2：Cache 查询无法靠 metadata 过滤
`GET /dataset-run-items` 仅支持 `datasetId / runName / page / limit`，**不支持按 metadata 字段过滤**。

**影响**：cache key `(prompt_name, prompt_version, dataset_item_id, judge_model)` 不能直接 SQL-like 查询。

**应对方案**：
- 把关键字段编进 `runName`，例如 `ab-baseline__intention-prompt__v17__judge-qwen-max__20260510T120000Z`
- `GET /datasets/{name}/runs` 列出所有 run，客户端按 runName 前缀过滤
- 命中候选 run 后再 `GET /datasets/{name}/runs/{runName}` 拿 items 列表，本地按 datasetItemId 求交集
- **代价**：列 runs 是 O(N)，N 大了得分页拉。可以加个本地 LRU 减少调用

#### 坑 #3：Run-item 没有 metadata 字段
按 OpenAPI spec，`DatasetRunItem` 仅有 `id / datasetRunId / datasetRunName / datasetItemId / traceId / observationId / createdAt / updatedAt`，**没有 metadata**。

**影响**：原方案想在 candidate run 里给每个命中 item 标 `cache_hit: true / cache_source_run_id: ...`，无处可挂。

**应对方案**：把 cache 命中信息聚合到 **Run 级 metadata**：
```json
{
  "cached_count": 30,
  "executed_count": 3,
  "cache_source_run_ids": ["ab-baseline__...__20260509T..."],
  "cached_item_ids": ["item-1", "item-7", ...]
}
```
丢失的是"逐 item 命中证据"，但聚合够用。

#### 坑 #4：Run metadata 是覆盖式更新
spec 原文："metadata ... updates run if run already exists"。**每次 POST item 时附带的 metadata 都会覆盖 run.metadata**（行为待实测——可能是 patch 也可能是 replace，需要在第一次实操时验证）。

**应对方案**：
- 第一个 item POST 时带完整 metadata
- 后续 item POST 时**省略 metadata**，避免反复触发更新
- 评估全部跑完后，**额外**做一次 POST 把最终聚合 metadata（如 `pass_rate / regressions / safe_to_upgrade`）写上——但需要选一个 item 作为载体。可以指定第一个 item 重 POST。
- ⚠️ **未确认**：重 POST 同 `(runName, datasetItemId)` 是创建新 item 还是更新旧 item？需要实测。

#### 坑 #5：traceId 必填 → PromptFoo 必须先把 trace 写进 Langfuse
A/B 跑出来的样本是新建的，PromptFoo 默认不写 Langfuse。要创建 run-item 必须先有 traceId。

**应对方案**：
- 选项 A：在 PromptFoo 配置里挂 Langfuse callback，让 PromptFoo 评估时自动生成 trace（PromptFoo 0.121+ 支持 `tracing.langfuse`，但需要确认）
- 选项 B：评估结束后从 PromptFoo 的 JSON 输出读取每条用例的输入输出，**人工 POST `/api/public/traces` 创建伪 trace**，再创建对应的 run-item
- 选项 A 干净但需要给 promptfooconfig.yaml 加配置；选项 B 实现独立但有"伪 trace"语义不洁的问题
- **倾向选 A**，待确认 PromptFoo 的 Langfuse tracing 实际可用性

### 设计层新问题：eval-online 的 dataset item 关联

> **状态**：2026-05-10 用户给出了三层数据集架构，本节问题被取代。详见下一节。

eval-online 评的是**线上流量**，trace 来自生产但不对应任何 golden dataset item，但 `POST /dataset-run-items` 必填 `datasetItemId`。讨论过 sentinel item / 逐条建 item / 独立 dataset 三个方案，因架构升级被废弃。

---

## 阶段 1.5：架构升级 — 三层长期数据集

**时间**：2026-05-10
**起因**：用户提出更长远的数据集治理方案，覆盖了 sentinel 等单点权宜之计

### 三层数据集

每个 agent 维护三个独立 dataset：

| Dataset | 命名 | 用途 | 写入策略 |
|---------|------|------|----------|
| 黄金 | `{agent}-golden` | 当前能力基线，A/B 主战场 | 慢迭代，手工增删 |
| 回归 | `{agent}-regression` | 历史 bug 沉淀，CI 必跑 | 只增不删（bug 修复彻底后才人工删） |
| 线上临时 | `{agent}-online-temp` | eval-online 工作区 | **跑前清空**，覆盖式写入 |

**核心好处**：
- online 数据膨胀问题消失（覆盖式控制在临时集）
- 线上发现的好 case 有显式沉淀通道：`online-temp` → 手工选 → `golden` / `regression`
- A/B 评估可叠加 golden + regression 两层门禁

### 已敲定的决策（2026-05-10）

| # | 决策 | 选择 |
|---|------|------|
| 1 | 命名规范 | `{agent}-golden` / `{agent}-regression` / `{agent}-online-temp` |
| 2 | 现存 dataset 处理 | **直接重命名**为 `*-golden`（不保留旧名做别名） |
| 3 | promote 工具 | **先做 CLI** `eval-dataset-promote`（不靠 UI 手工） |
| 4 | A/B 跑哪些 dataset | 默认只跑 `golden`，`regression` 可选；任一失败 = `A/B ❌` |
| 5 | online-temp 写入策略 | **跑前清空**（不保留历史批次） |

### 对原设计的影响

| 原计划 | 调整为 |
|--------|--------|
| eval-online 用 sentinel item ❌ | 改用 `{agent}-online-temp` 独立 dataset，每条 trace 一条 item |
| A/B 和 online 同 dataset 复用 ❌ | 解耦：A/B 在 golden(+regression)，online 在 online-temp |
| 单一 cache key 服务 A/B + online | 仅 A/B 用 cache（item 稳定），online 不缓存（每次 item 新建） |
| `dspy-optimize.yaml` | 新增 `ab.datasets: [<agent>-golden, ...]` 字段 |
| 新增 CLI | `eval-dataset-promote`（online-temp item → golden/regression） |
| 现存 dataset 迁移 | 需写迁移脚本：建 *-golden + 复制 items + 业务项目改名 |

### 现存 dataset 迁移步骤（待落实）

当前 Langfuse 实际有的 dataset：`intention` / `recommend` / `replenish`（混合用途）。迁移：

1. 对每个 agent：`POST /datasets {name: "{agent}-golden"}`、`{agent}-regression`、`{agent}-online-temp`
2. 把旧 `{agent}` 的所有 items 复制到 `{agent}-golden`
3. 业务项目 `eval-ai-order` 全局改名（agents/*/promptfooconfig.yaml、dspy-optimize.yaml、eval-online.yaml）
4. 旧 `{agent}` dataset 留作只读备份，不再写入；后续手工删

→ 这一步会作为阶段 6 单独执行，一次性 CLI `eval-migrate-datasets-v2`

### 仍待解的开放问题（不阻塞阶段 2）

1. **坑 #5**：PromptFoo trace 走 A（原生 callback）还是 B（评估后伪造 trace）？阶段 4 改造 `promptfoo_ab.py` 时再决定
2. **坑 #4 实测**：metadata 是覆盖还是合并、重 POST 同 (runName, datasetItemId) 是否更新——阶段 2 第一次实操时验证

### 阶段 2 摸底补充（2026-05-10）

进一步扒了 `dataset-items.yml` / `trace.yml`，又发现两件事：

**🎁 好消息**：`CreateDatasetItemRequest` 自带 `sourceTraceId` / `sourceObservationId` 字段（Langfuse 原生支持「这个 dataset item 来源于某条 trace」语义）。

**影响**：eval-online 把线上 trace 写为 `online-temp` 的 item 时，直接填 `sourceTraceId=<生产 trace_id>`，Langfuse UI 会展示 dataset item ↔ 来源 trace 的链接。无需在 metadata 里自己实现这层关联。

**🚨 坑 #6（新增）**：Public REST API **没有 `POST /traces`** 端点
trace 在 Langfuse 通过 `/ingestion` 异步管线进入，公共 REST API 只有 `GET / DELETE /traces/{id}`、`GET /traces`，**没有同步创建 trace 的能力**。

**影响**：坑 #5 的「选项 B：评估后伪造 trace」需要走 ingestion 协议，复杂度高（要构造 trace + observation 事件 batch）。**坑 #5 现在强烈倾向选项 A**（PromptFoo 原生 Langfuse callback）。如果 PromptFoo callback 不可用再考虑走 ingestion，但建议作为 fallback 不优先。

**🛠️ DELETE 端点全部确认存在**：
- `DELETE /dataset-items/{id}` —— 单条删（用于 online-temp 清空）
- `DELETE /datasets/{name}/runs/{name}` —— 整 run 删（含所有 items）
- `DELETE /traces/{id}` —— 单 trace 删（不在本次改造范围）

**没有"批量清空 dataset" API**：清空 online-temp 必须逐条 DELETE，100 条 = 100 次调用。需要在 LangfuseClient 封装一个 `delete_all_dataset_items()` 高层方法，量大时再考虑并发。

---

## 阶段 2：扩展 LangfuseClient（已完成 2026-05-10）

新增 9 个方法（[langfuse_client.py](../src/eval_shared/common/langfuse_client.py)）：

```
delete_dataset_item(item_id)
delete_all_dataset_items(dataset_name) → int                # 高层封装：列+逐条删
create_dataset_run_item(*, run_name, dataset_item_id, trace_id, ...)
list_dataset_run_items(*, dataset_id, run_name, ...)
list_dataset_runs(dataset_name, ...)
get_dataset_run(dataset_name, run_name)
delete_dataset_run(dataset_name, run_name)
```

测试覆盖（[tests/test_langfuse_client.py](../tests/test_langfuse_client.py)）：6 个新测试，全量 14 pass。

### 阶段 2 中的小坑

- 现有 `FakeHttpClient` 只 mock 了 `get`，新方法涉及 `post / delete`。新建 `FakeMultiMethodHttp` 类支持三种方法+多 URL 响应队列，避免改动现存测试。
- `delete_all_dataset_items` 当前实现是串行 DELETE，每条 item 一次 API 调用——在线上量大时（百条以上）需要改并发或加进度条。先标 TODO 不优化。
- **坑 #4 行为仍未实测**：metadata 覆盖 vs 合并、重 POST 同 (runName, datasetItemId) 的行为，要等阶段 4 实操时验证。当前 `create_dataset_run_item` 的 docstring 已经写了「建议仅第一个 item 携带 metadata」的保守用法。

---

## 阶段 3：A/B 缓存查询 helper（已完成 2026-05-10）

新模块 [common/dataset_run_cache.py](../src/eval_shared/common/dataset_run_cache.py)，导出：

```
CacheKey(prompt_name, prompt_version, judge_model, role)
CacheLookupResult(hits, miss_item_ids, source_run_names)
build_run_name(key, ts=...)            # 含时间戳的完整 run 名
build_run_name_prefix(key)             # 用于命中查询的前缀
lookup_cache(client, dataset_name, target_item_ids, cache_key)
```

**核心设计决策**（已与用户敲定）：

1. **score 不复制，复用 trace_id**：命中后新 run-item 的 traceId 复用历史 trace。Langfuse 通过 trace 级联自动聚合 score 到新 run，零冗余。
2. **多 run 命中同 item 时取最新**：客户端按 `createdAt` 倒序排序候选 run。Langfuse REST API 没文档化 list 排序，靠客户端兜底。
3. **早终止**：targets 全部命中后立即停止遍历，避免无谓 GET。

### 阶段 3 中的小坑

- `list_dataset_runs` 文档没明说排序。**保险做法**：客户端拿到所有 runs 后用 `createdAt` 字段重排，不依赖服务端默认顺序。
- `judge_model` 等字段含 `:` `/` 等特殊字符（如 `openai:gpt-4o`）会破坏 URL 路径。`_sanitize` 用正则 `[^A-Za-z0-9_-]` 替换为 `-`。**副作用**：`openai:gpt-4o` 与 `openai-gpt-4o` 两个不同字段会冲突 sanitized 成同值，但 judge_model 实际不会同时出现这两种写法，先不管。
- 单 run GET 失败（如被并发删除）不应阻断整次缓存查询。`get_dataset_run` 抛异常时跳过当前 run 继续。

测试覆盖（[tests/test_dataset_run_cache.py](../tests/test_dataset_run_cache.py)）：10 个用例覆盖编码 / sanitize / 全 miss / 全 hit / 部分 hit / 多 run 取最新 / 跨版本不命中 / 早终止 / 异常跳过。全量 24 pass。

---

## 阶段 4 拆分

阶段 4 涉及 4 个文件改造，按依赖顺序拆为 4a-4f 子阶段：

| 子阶段 | 文件 | 状态 |
|--------|------|------|
| 4a | `common/ab_verdict.py` 新建 + `dspy_pipeline._annotate_prompt` 三态 | ✅ |
| 4b | `promote_prompt.py` 门禁改枚举集合 + promote 剥离 A/B 状态 label | ✅ |
| 4c | `promptfoo_ab.py` 多 dataset + 容忍阈值（SAME 判定） | ⏳ |
| 4d | spike：PromptFoo Langfuse tracing 集成可行性（坑 #5） | ⏳ |
| 4e | `promptfoo_ab.py` 接入 cache + Dataset Run 写入 | ⏳ |
| 4f | `eval_online.py` 清空 online-temp + 写 Dataset Run | ⏳ |

## 阶段 4a：ABVerdict 三态枚举（已完成 2026-05-10）

新增 [common/ab_verdict.py](../src/eval_shared/common/ab_verdict.py)：

```
ABVerdict.BETTER = "A/B ✅"
ABVerdict.WORSE  = "A/B ❌"
ABVerdict.SAME   = "A/B 🟰"
AB_VERDICT_LABELS  # frozenset，promote 时剥离用
verdict_from_ab_summary(summary)  # 从 promptfoo-ab summary 推断 verdict
```

判定规则（无 tolerance，4c 后再加）：
1. `safe_to_upgrade=True` → BETTER
2. `regressions>0` 或 `rate_diff<0` → WORSE
3. 其他 → SAME

`dspy_pipeline._annotate_prompt` 改造：
- 签名从 `(prompt_name, ab_summary, dspy_report)` 改为 `(prompt_name, verdict)`
- label 写入只用枚举值（剥离全部 A/B 状态 label 后 append 新 verdict）
- DSPy 短路跳过 A/B 时也标 🟰（之前漏标，留着旧 label 误导后续 promote）

测试：[test_ab_verdict.py](../tests/test_ab_verdict.py) 9 cases。

## 阶段 4b：promote_prompt 门禁改枚举（已完成 2026-05-10）

[cli/promote_prompt.py](../src/eval_shared/cli/promote_prompt.py) 改造点：

1. **门禁逻辑**：精确枚举 `ABVerdict.WORSE.value` 阻断 + 兼容旧 `"A/B ❌ 67.7%→..."` 前缀格式
2. **🟰 警告但不阻断**：候选相当于基线时给提醒，让人知情后自决
3. **promote 剥离全部 A/B 状态 label**：包含新枚举 + 旧数字明细格式（`startswith("A/B ")`），让 production 版本不带评估状态
4. **`--force` 也剥离**：之前 force 通过会让 `A/B ❌` 跟着上 production，新逻辑修正

测试 [test_promote_prompt.py](../tests/test_promote_prompt.py) 从 4 个扩到 9 个，覆盖新枚举 / 旧格式 / SAME 警告 / 非相关 label 保留 / dry-run 也走门禁。

### 阶段 4a/4b 中的小坑

- **中文标点匹配**：用 Edit 工具改 docstring 时用错半角标点（`,` vs `，`、`:` vs `：`）导致 old_string 匹配失败。**应对**：必须从 Read 工具的输出原样拷贝。
- **旧 label 兼容性**：历史 prompt 上残留的 `A/B ✅ 70.0%→80.0%`（带数字明细）不在新枚举集合内，需要 `startswith("A/B ")` 自动清理。门禁也要兼容 `startswith("A/B ❌")` 防止旧失败 label 漏阻断。**未来收益**：跑过一轮新版 promote 后，所有版本的 label 自动收敛到 3 态枚举集，旧格式自然消失。
- **--force 兜底剥离**：用户之前测试期望 `A/B ❌` 跟着上 production（force 不剥离），新规则改成无论是否 force，production 都不带评估状态——这是更安全的默认。

测试：全量 38 pass（之前 14，+24 新增）。

---

## 阶段 4c：tolerance + summary verdict 字段（已完成 2026-05-10）

**范围调整**：原计划 4c 包含多 dataset 实跑，但发现实跑依赖业务项目的 dataset 配置改名（阶段 6 才迁移），所以 4c 只做：
- tolerance 容忍阈值（让 SAME 三态实际可用）
- summary.json 顶层加 `verdict` + `tolerance` 字段
- `aggregate_verdicts()` 多 dataset 聚合函数（提前实现，阶段 6 后用）

多 dataset 实跑改造移到阶段 6（与 dataset 重命名同步进行）。

**改动点**：

| 文件 | 改动 |
|------|------|
| `common/ab_verdict.py` | 新增 `compute_verdict(rate_diff, regressions, improvements, tolerance)` 和 `aggregate_verdicts(verdicts)`；`verdict_from_ab_summary` 优先读顶层 `verdict` 字段 |
| `cli/promptfoo_ab.py` | CLI 加 `--tolerance` 选项（默认 1.0%）；summary.json 增加 `verdict` 和 `tolerance` 字段；终端结论改三态文案 |
| `cli/dspy_pipeline.py` | `dspy-optimize.yaml` 的 `ab.tolerance` 透传到 `eval-promptfoo-ab --tolerance` |

**判定规则（compute_verdict）**：
1. `regressions > 0` 或 `rate_diff < -tolerance` → WORSE
2. `rate_diff > tolerance` 且 `improvements > regressions` → BETTER
3. 其他 → SAME

**多 dataset 聚合（aggregate_verdicts）**：取最差。任一 WORSE → 整体 WORSE；全部 BETTER → 整体 BETTER；其他 SAME。空列表保守归 SAME。

### 阶段 4c 中的小坑

- **`is_safe_to_upgrade` vs `compute_verdict` 语义不一致**：旧 `is_safe_to_upgrade` 没 tolerance（rate_diff=0.5% 也算 safe），`compute_verdict(tolerance=1.0)` 会判 SAME。决定：保留 `is_safe_to_upgrade` 兼容旧调用，新代码统一用 `compute_verdict`。summary.json 同时输出两者。
- **summary 向后兼容**：`verdict_from_ab_summary` 加了"优先读顶层 verdict 字段"的快路径；老 summary 没这个字段时回退到推断逻辑。dspy_pipeline 不需改动就能兼容新旧两种 summary。
- **多 dataset 聚合 `min` 函数选 key**：`min(verdicts, key=lambda v: _VERDICT_RANK[v])` 取的是 rank 最小（即 WORSE=0），符合"最差优先"语义。第一次写成 `min(verdicts)` 直接比较 enum 会按字母序排，结果错误。

测试：`test_ab_verdict.py` 9 → 18 cases，全量 47 pass。

---

## 阶段 4d：PromptFoo Langfuse tracing spike（已完成 2026-05-10）

调研三个候选方案，判断坑 #5 怎么解决。

### 候选方案对比

| 方案 | 可行性 | 实施成本 | 维护成本 |
|------|--------|----------|----------|
| A. PromptFoo 原生 Langfuse callback | ❌ 不可行 | — | — |
| B. PromptFoo afterEach hook（JS）调 Langfuse SDK | ✅ 可行 | 中（要改业务项目 promptfooconfig） | 中（hook 脚本要分发） |
| C. promptfoo_ab.py 评估完后自己解析 JSON → ingestion API | ✅ 可行 | 低 | 低 |

**A 方案否决**：调研 PromptFoo 文档明确说 Langfuse 集成只支持**入站**（用 `langfuse://` 前缀引用 prompt），**不支持出站**写 trace。

**B 方案技术上可行但工程上不优**：PromptFoo 有 `afterEach` / `afterAll` 扩展 hook（JS/Python），可以在每条 case 跑完时回调。但这会侵入业务项目 promptfooconfig.yaml，且 hook 脚本要分发——破坏 eval-shared 与业务项目的边界。

**C 方案胜出**：保持改造收敛在 eval-shared，业务项目零改动。

### 选定方案：C — 评估后批量构造 ingestion 事件

```
PromptFoo 评估 → output JSON
  ↓
promptfoo_ab.py 解析 JSON 每条 case：
  ↓ 给每条 case 自生成 UUID v4 作为 traceId
构造 ingestion batch:
  - trace-create  事件 × N: id=traceId, input=vars, output=response.output
  - score-create  事件 × N: traceId, value=success ? 1 : 0
  ↓
POST /api/public/ingestion
  ↓ 检查 207 响应的 errors
  ↓
为每条 case 创建 dataset-run-item(runName, datasetItemId, traceId)
```

### Langfuse Ingestion API 关键事实

来自 `langfuse/langfuse:fern/apis/server/definition/ingestion.yml`：

- **POST `/api/public/ingestion`**，请求体 `{batch: list<IngestionEvent>}`
- **批量上限 3.5 MB**，超出需要分批
- **响应 207 multi-status**：成功条目在 `successes`，失败在 `errors`，**不会返回 4xx**——必须检查 errors
- **事件类型**：`trace-create` / `span-create` / `score-create` / `observation-create` 等（discriminated union by `type`）
- **幂等**：相同 `body.id` 重复 POST 会 upsert
- **trace_id 客户端自生成**：标准 Langfuse SDK 都是客户端生成 UUID，服务端 upsert 接受

### 🚨 坑 #7（新增）：Ingestion API 标记为 deprecated

OpenAPI spec 原文：
> "Use the OpenTelemetry endpoint at /api/public/otel/v1/traces instead. Learn more: https://langfuse.com/integrations/native/opentelemetry"

**判定**：
- deprecated 不等于删除——Langfuse 仍在维护这个端点供老 SDK 使用
- 用户的 Langfuse 是自部署版本，长期内 ingestion 仍可用
- **现在用 ingestion 是合理选择**，OTel 路线需要构造 OTel proto 格式或起 collector，复杂度高出一个数量级
- **技术债**：当 Langfuse 真正删除 ingestion 时（暂无时间表），需要迁移到 `/otel/v1/traces`。当前在 LangfuseClient 内部封装 `write_traces_via_ingestion()` 时把这个备注写进去

### 阶段 4e 实现要点（提前梳理）

LangfuseClient 需要再加：
- `write_ingestion_batch(events: list[dict]) -> dict` —— POST 单批，返回 207 响应
- `submit_traces(traces: list[dict]) -> list[str]` —— 高层封装：自分批（3.5MB）+ 检查 errors + 返回 traceIds
- 同样需要 `submit_scores(scores: list[dict]) -> None` 高层封装

新一坑（提前预警）：
- **3.5MB 分批**：33 条用例 × ~3KB/条 ≈ 100KB，远小于上限。但批量评估可能上千条，要做客户端分批。
- **errors 处理**：207 + 部分成功部分失败的场景。决定：单条失败直接 raise，提前发现问题；不做"部分成功"的吞没。

---

## 阶段 4d.5：实操 spike — 验证 Langfuse 灰色行为（已完成 2026-05-10）

实操脚本：[tests/spike_ingestion_dataset_run.py](../tests/spike_ingestion_dataset_run.py)
（pytest 不收集 `spike_*.py`，是手动探针，将来排查问题也能复用）

### 实测结果（4/4 验证完成）

| Q | 验证项 | 期望（按 spec） | 实测 | 严重度 |
|---|--------|-----------------|------|--------|
| Q1 | 同 `body.id` 重 POST trace 是否 upsert | upsert | **first write wins** | 🚨 重要 |
| Q2 | run.metadata 多次 POST 是覆盖/合并 | "updates run if run already exists" | **first write wins** | 🚨 重要 |
| Q3 | 重 POST 同 (runName, datasetItemId) | spec 未说 | **update in place（traceId 被新值替换）** | ✅ 好消息 |
| Q4 | ingestion 响应格式 | 207 + 多状态 | **207 + `{successes, errors}`，业务错误异步吞掉** | ⚠️ 需注意 |

### 🚨 坑 #8（新增 — 最大影响）：异步索引延迟 ~10-15s

**所有 GET 操作在写入后必须等待**：
- POST trace 后 GET trace 需轮询 5-15s
- POST run-item 后 GET run 含 items 需轮询 5-15s
- spike 脚本里加了 `_retry_until` helper 才稳定

**对 4e 实现的影响**：写完不能立即 GET 验证，要么轮询、要么直接信任 POST 的同步返回值。

### 🚨 坑 #9（新增）：ingestion + run metadata 都是 first-write-wins

跟 spec 描述**不一致**。可能解读：
- ingestion 在异步处理时若已存在同 id trace，可能把后续事件归类为"已处理"丢掉
- run metadata 通过 POST run-item 的 metadata 字段更新——一旦 run 创建，后续 POST 的 metadata 字段被忽略

**这是关键约束**，对方案影响很大：

❌ **不能用的模式**：
- 跑评估时实时写 run-item，最后再 POST 一次"总结" metadata（如 pass_rate / safe_to_upgrade）—— 后续 metadata 写不进去
- 用 trace_id 占位先 POST，等评估完后再 POST 真实 trace 内容 —— input/output 写不进去

✅ **必须用的模式**：
- 评估**完整跑完** → **算好所有数据** → 再开始写 Langfuse
- 第一个 POST run-item 携带**完整最终的** metadata（含 pass_rate 等）
- 后续 run-items 不带 metadata
- trace UUID 一次性生成，第一次 POST 即包含完整 input/output/metadata

### ✅ 坑 #4 部分解决（坏消息变好消息）

之前坑 #4 担心"重 POST 同 (runName, datasetItemId) 行为不明"——实测结果是 **update in place**，traceId 被新值覆盖。

**应用**：A/B cache 命中复用旧 trace_id 时，可以放心 POST 新 run-item，不会出现重复条目。但 POST 的是"新 run-item id"——历史 run 里那条不会变。

### ⚠️ 坑 #10：ingestion 业务错误异步吞掉

故意 POST 一条引用不存在 traceId 的 `score-create`，结果：
- HTTP 207
- successes 4 条（包括错误那条！）
- errors 0 条

**意味着 POST 响应不能用来判定 score 是否真的写入**。要 GET trace 验证才知道。但生产场景下，UUID v4 撞概率为 0，构造的 score 都引用刚 POST 的 trace，正常情况不会有这种错误。**风险可接受，不做特殊处理**。

### 4e 实现要点（基于 spike 结果调整）

```
A/B 流程：
1. 跑两次 PromptFoo（baseline + candidate）→ 拿到 JSON 输出
2. 解析全部结果，计算 baseline_stats / candidate_stats / regressions / improvements / verdict
3. 缓存查询（lookup_cache）找到可复用的历史 trace_ids
4. 对 miss 的 case：UUID v4 自生成 traceId
5. 构造 ingestion batch：trace-create × N（miss 才发） + score-create × N（pass/fail 二值）
6. POST /api/public/ingestion（按 3.5MB 分批）
7. （可选）轮询 GET 一条 trace 确认异步处理就绪，再继续——**或者直接信任 POST 同步返回**
8. POST 第一个 run-item 携带完整 metadata（含所有评估结论）
9. POST 剩余 run-items（不带 metadata）
10. 命中的 case：traceId 直接复用，POST run-item，不发 ingestion
```

**简化决策**：第 7 步可以省掉——POST run-item 时即使 trace 还没异步索引完成，Langfuse 的 run-item 写入也能成功（POST 同步生效）。视觉上短期 UI 看不到 trace 详情，等 ~15s 后自动显示。

---

## 阶段 4e：promptfoo_ab.py 接入缓存 + Dataset Run 写入（已完成 2026-05-11）

按用户决定走 **A 方案**（真省 LLM 调用），实施分 5 个子阶段：

| 子阶段 | 改动 |
|--------|------|
| 4e-1 | `dataset_run_cache.fetch_scores_by_trace_id` + `LangfuseClient.list_scores` 支持 v2 query (traceId/datasetRunId 过滤) |
| 4e-2 | `common/dataset_item_id.compute_item_id` 抽出共享算法（sync_dataset 改 import）+ `common/promptfoo_subset.*` (filter/write/cleanup 临时 dataset+config) |
| 4e-3 | `LangfuseClient.submit_ingestion_batch` + `common/ingestion.*` (build_trace_event / build_score_event / new_trace_id)，约定 `score name=promptfoo_pass, dataType=NUMERIC, value=1.0/0.0` |
| 4e-4 | `promptfoo_ab.py` 主流程重写：拉 prompt → 缓存查询 → miss-only 子集跑 → 合并 hit+miss → 写 Dataset Run；新增 `_run_promptfoo_subset` / `_merge_hit_and_miss` / `_build_run_metadata` / `_write_langfuse_run`；CLI 加 `--no-cache` |
| 4e-5 | 测试覆盖 (12 个新测) |

### 关键设计点

- **PromptFoo 子集跑实现**：临时 dataset YAML + 临时 promptfooconfig.yaml 放在 `agents/<agent>/` 下（保持相对路径解析）；`.` 前缀便于 .gitignore；跑完 finally 块清理
- **cache key 写入 runName**：`ab-{role}__{prompt_name}__v{version}__judge-{judge_safe}__{ts}` —— Langfuse REST 不支持 metadata 过滤，靠 runName 前缀匹配做命中查询
- **hit 复用历史 trace_id**：新 dataset run-item 关联老 trace；不发新 ingestion；Langfuse UI 上 trace 同时归到新旧两个 run，pass_rate 自动通过 trace 级联聚合
- **first-write-wins 约束**：run.metadata 必须在第一个 POST run-item 时一次性传完（含 verdict / pass_rate / cached_count 等所有评估结论）。`_write_langfuse_run` 严格遵守
- **`is_safe_to_upgrade` 保留**：旧布尔接口兼容老调用方；新代码用 `compute_verdict` 拿三态枚举
- **summary.json 新字段**：`verdict` / `tolerance` / `cache` / `langfuse_run_names`

### 阶段 4e 中的小坑

- **Edit 工具中文标点匹配多次失败**：4a / 4e-4 都踩了。原文是 `（）：，` 全角，我容易抄成 `()：，` 半角。**应对**：必须从 Read 输出原样拷贝；分小段 Edit 避开标点风险
- **`_pull_prompt_to` 签名变了** (None → int)：返回 prompt 版本号给 cache key 用，无 fallback
- **`compute_item_id` 共享**：sync_dataset 从 inline 改 import；保证 push 时算的 id 与 promptfoo_ab 复算 id 严格一致，否则缓存永远 miss
- **`is_safe_to_upgrade` vs `compute_verdict` 容忍语义不同**：summary 顶层同时输出二者，dspy_pipeline 优先读 verdict 字段（4c 已做兼容）
- **mock 主流程难度**：`_write_langfuse_run` 写测试时需要 mock 一个支持 `submit_ingestion_batch` + `create_dataset_run_item` 的轻量 LangfuseClient stub；放在 test_promptfoo_ab.py 里就近管理

测试：`test_promptfoo_ab.py` 2 → 14 cases；全量 47 → 82 (+35)。

---

## 阶段 4f：eval_online 改造（已完成 2026-05-11）

按用户「跑前清空 + 覆盖式」策略，eval_online 在保留原 trace-level Score 写回的基础上，
**额外**写入 `{agent}-online-temp` Dataset Run。

### 改动点

| 文件 | 改动 |
|------|------|
| `cli/eval_online.py` | 加 `_online_dataset_name()` / `_online_run_name()` helper；evaluator 配置新增可选 `agent` 字段；启动时清空 online-temp datasets；evaluator 跑完写 Dataset Run（含 metadata：source/agent/score_name/judge_model/time_window/pass_rate 等） |
| `tests/test_eval_online.py` | 新增 2 个命名 helper 测试 |

### 关键设计点

- **向后兼容**：evaluator 不带 `agent` 字段 → 完全走旧流程（只写 trace score）；带了 → 额外写 Dataset Run。已有 yaml 配置零破坏迁移。
- **跑前清空**：主流程启动时收集所有 evaluator 用到的 agent，按 `{agent}-online-temp` 逐个调用 `delete_all_dataset_items` 清空（覆盖式语义由 known-issues #4 决定——Langfuse 不支持删 dataset，只能逐条 DELETE item）。
- **dataset item 用 `sourceTraceId`**：CreateDatasetItemRequest 原生字段，UI 上能直接跳回生产 trace 看明细，不需要在 metadata 里自己实现这层关联。
- **每个 evaluator 一个 Dataset Run**：runName 用 `online-{agent}-{score_name}-{ts}`，前缀 `online-` 区分 A/B 的 `ab-baseline/ab-candidate`，避免被 cache 查询误命中。
- **trace-level Score 保留**：原有的 `client.write_score(...)` 不变，单 trace 详情页仍能直接看 score。Dataset Run 是聚合视图的额外通道，两者并行不冲突。
- **first-write-wins 约束**：run-item POST 时仅第一个携带完整 metadata（含 pass_rate / total_evaluated 等），后续不带——与 4e 一致。

### 阶段 4f 中的小坑

- **`_process_evaluator` 签名加了 `hours` 参数**：metadata 里要记录评估窗口长度，原签名没传。调用方 `main` 同步加传。
- **多 evaluator 共享同一 agent dataset**：如果 yaml 里两个 evaluator 都标 `agent: intention`，启动时只清一次（用 `set` 去重），但每个 evaluator 写各自独立的 Dataset Run。这样同一 dataset 下能并排展示「意图判定」「推荐质量」两个评估器的趋势。
- **dataset item id 由 Langfuse 自分配**：跟 sync_dataset 不同（那里用 SHA1(vars) 算 id），eval-online 不指定 `id` 字段，让 Langfuse 自动生成 UUID。原因：online trace 输入是连续流量，没有"同 vars 应该是同 item"的语义。

测试：`test_eval_online.py` 新增 2 测试；全量 82 → 84。

---

## 阶段 5：eval-dataset-promote CLI（已完成 2026-05-11）

新增 [cli/dataset_promote.py](../src/eval_shared/cli/dataset_promote.py) + 注册到 pyproject.toml。
LangfuseClient 加 `get_dataset_item(item_id)` 单条 GET。

### 核心行为

```bash
eval-dataset-promote --agent intention --to {golden|regression} \
    --item-ids id1,id2 --reason "..."
```

- 源默认 `{agent}-online-temp`，可用 `--from` 覆盖
- 目标 `{agent}-{to}`，不存在则自动创建
- 复制 input/expectedOutput，metadata 加 `promoted_from / promoted_from_item_id / promoted_at / promoted_reason`
- 目标 item.id 用 `compute_item_id(target_dataset, src.input)` 复算——与 sync_dataset push 算法一致，避免后续 sync 时重复 push 同一条
- 源 item **不删除**（让 eval-online 下次跑自动覆盖清理）
- `--list` 查看可选 item；`--dry-run` 干跑

测试：[test_dataset_promote.py](../tests/test_dataset_promote.py) 9 cases，全量 84 → 93。

---

## 阶段 6a：eval-migrate-datasets-v2 CLI（已完成 2026-05-11）

新增 [cli/migrate_datasets_v2.py](../src/eval_shared/cli/migrate_datasets_v2.py)，注册到 pyproject。

用法：
```bash
eval-migrate-datasets-v2 --agent intention                     # 单 agent
eval-migrate-datasets-v2 --all                                 # 扫 agents/ 全部
eval-migrate-datasets-v2 --agent intention --from-name legacy  # 覆盖源 dataset 名
eval-migrate-datasets-v2 --all --dry-run                       # 干跑
```

对每个 agent：
1. 拉旧 dataset items → 复制到 `{agent}-golden`（item.id 用 `compute_item_id` 复算，保证 sync 幂等）
2. 建空 `{agent}-regression`、`{agent}-online-temp`
3. 旧 dataset **不删除**——人工确认后手工删

迁移每条 item 的 metadata 加：`migrated_from / migrated_from_item_id / migrated_at`。

测试：[test_migrate_datasets_v2.py](../tests/test_migrate_datasets_v2.py) 8 cases，全量 93 → 101。

## 阶段 6b：sync_dataset 支持 typed datasets（已完成 2026-05-11）

[cli/sync_dataset.py](../src/eval_shared/cli/sync_dataset.py) 改造：

| 改动 | 细节 |
|------|------|
| 加 `--type` 选项 | golden / regression / online-temp，默认 golden |
| 默认 dataset 名 | `{agent}-{type}` (替代旧的 `{agent}`) |
| 本地 YAML 路径 | `agents/{agent}/datasets/{type}.yaml` |
| `--dataset` 选项 | 保留作为覆盖默认名的应急 escape hatch |
| push online-temp 警告 | online-temp 是 eval-online 工作区，push 通常没意义，警告提示 |

新 helper：`_default_dataset_name(agent, type_)` + `_local_path(agent, type_)`。

测试：[test_sync_dataset.py](../tests/test_sync_dataset.py) 4 cases（新增），全量 101 → 105。

## 阶段 6c：promptfoo_ab 默认 dataset = {agent}-golden（已完成 2026-05-11）

**范围调整**：原计划做"多 dataset 并行 + 聚合 verdict"，评估后觉得改动太大（300+ 行 + 风险高）。
当前用户也没真有同时跑 golden + regression 的紧迫场景。改为**最小版**：

- `promptfoo_ab.py` 默认 dataset 改为 `{agent}-golden`（之前是 `{agent}`，迁移后必须改）
- 加 `--dataset` 选项：用户可手工指定其他 dataset（如 `intention-regression` 单独跑一次）
- 新 helper `_infer_local_dataset_path` 从 dataset 名约定推断本地 YAML 路径
- dspy_pipeline 透传 `ab.dataset` 配置项到 CLI（与 `ab.tolerance` 一起）
- header 输出加 dataset 字段，让人知道在跑哪份

真正的多 dataset 并行/聚合留作 future work（用户若有 CI 跑 golden + regression 一起的需求再实现）。
`aggregate_verdicts` 已经在 4c 提前实现好了，等真要做时直接 wire。

测试：[test_promptfoo_ab.py](../tests/test_promptfoo_ab.py) 加 `_infer_local_dataset_path` 3 cases，全量 105 → 108。

## 阶段 6d：业务项目（eval-ai-order）迁移清单

> ⚠️ 以下步骤需要按顺序执行。先在 dev 环境跑一遍验证再推 prod。

### 0. 前置：升级 eval-shared

```bash
cd /path/to/eval-shared
git pull
uv pip install -e ".[dev,dspy]"  # 或 pip install -e .
```

### 1. 一键迁移 Langfuse dataset

```bash
cd /path/to/eval-ai-order
eval-migrate-datasets-v2 --all --dry-run    # 先看会做什么
eval-migrate-datasets-v2 --all              # 实际执行

# 验证：Langfuse UI 上每个 agent 应能看到 4 个 dataset：
#   intention / intention-golden / intention-regression / intention-online-temp
#   旧 intention 不删除，留作只读备份
```

### 2. 改 agents/*/dspy-optimize.yaml

把 `dataset:` 字段从旧名改成新名：

```diff
- dataset: intention
+ dataset: intention-golden
```

三个 agent 都要改（intention / recommend / replenish）。

### 3. 改 eval-online.yaml

给每个 evaluator 加 `agent:` 字段以启用 Dataset Run 写入：

```diff
  evaluators:
    - name: "用餐意图识别"
+     agent: intention
      scoreName: "意图准确性"
      ...
    - name: "点餐LLM"
+     agent: recommend
      scoreName: "推荐合理性"
      ...
    - name: "补推LLM"
+     agent: replenish
      scoreName: "补推合规性"
      ...
```

不加 `agent` 字段也能工作（向后兼容），只是没有 Dataset Run 归档。

### 4. 验证 sync-dataset

```bash
# 现有 npm scripts 不变，但默认行为变了
npm run sync:datasets:pull     # 现在拉的是 intention-golden 等三个
# 本地 agents/*/datasets/golden.yaml 内容应与之前一致
```

### 5. 验证 A/B 流程

```bash
AGENT=intention npm run test:agent       # 跑 PromptFoo 单 agent
npm run promptfoo:ab -- intention        # 跑完整 A/B（含 Dataset Run 写入）

# 检查 output/intention-ab-summary.json 应含：
#   - verdict: "A/B ✅"/"A/B ❌"/"A/B 🟰"
#   - tolerance: 1.0
#   - cache: {baseline_hits, baseline_miss, candidate_hits, candidate_miss}
#   - langfuse_run_names: {baseline, candidate}
```

### 6. 验证 eval-online 写 Dataset Run

```bash
npm run eval:online -- --hours 1 --limit 5    # 小规模测试

# Langfuse UI:
#   Datasets / intention-online-temp / Runs → 应看到 `online-intention-意图准确性-...` 等 run
```

### 7. 验证 promote 路径

```bash
# Prompt label 现在自动是干净的三态枚举
npm run promote -- --agent intention
# production 版本上不带 A/B 状态 label
```

### 8. 删除旧 dataset（可选，确认无误后）

```
Langfuse UI → Datasets → 旧 `intention` / `recommend` / `replenish` → Delete
```

⚠️ **不可逆**。建议保留几周作为只读备份。

### 9. 业务项目 CHANGELOG 记一笔

eval-ai-order 的 CHANGELOG.md 加：
- 迁移到三层 dataset 架构
- 评估结果归档到 Langfuse Dataset Run
- Prompt label 自动收敛到三态枚举

---

## 实际执行记录（2026-05-11）

### Langfuse 数据迁移（已完成 ✅）

```
$ eval-migrate-datasets-v2 --all
```

| Agent | 复制 items | 新建 dataset | 备注 |
|-------|-----------|--------------|------|
| intention | 29/29 | golden / regression / online-temp | 全部 vars 唯一 |
| recommend | 9/9 | golden / regression / online-temp | 全部 vars 唯一 |
| replenish | **37/37** | golden / regression / online-temp | 7 条重复 vars 自动加 `_variant: 2/3/...` 去重，无丢失 |

合计：9 个新 dataset / 75 条 item / 0 错误。

迁移 metadata 字段：每条新 item 含 `migrated_from / migrated_from_item_id / migrated_at`；replenish 自动加 variant 的 7 条额外含 `variant_auto_assigned: true / variant_original_input`。

### eval-ai-order 业务配置改动（已完成 ✅）

| 文件 | 改动 |
|------|------|
| `agents/intention/dspy-optimize.yaml` | `dataset: intention` → `intention-golden` |
| `agents/recommend/dspy-optimize.yaml` | `dataset: recommend` → `recommend-golden` |
| `agents/replenish/dspy-optimize.yaml` | `dataset: replenish` → `replenish-golden` |
| `eval-online.yaml` | 3 个 evaluator 各加 `agent: intention/recommend/replenish`，启用 Dataset Run 写入；顶部字段说明加 `agent` 注释 |
| `CHANGELOG.md` | 新增 Migrated 2026-05-11 章节，记录数据迁移与配置改动 |

### 阶段 6c 后续小改（已完成 ✅）

迁移完成后实施了第二题的方案 ②：
- `promptfoo_ab.py` 加 `--sync-dataset` 选项，跑前从 Langfuse 拉最新 dataset 覆盖本地 YAML（默认关，CI 推荐开）
- `dspy_pipeline.py` 透传 `ab.sync_dataset` 配置项
- `migrate_datasets_v2.py` 加 `_assign_variant_for_duplicates` 自动处理 vars 重复（解决 replenish 7 条会丢失的问题）

测试：108 → 114（+6）。

---

## Pending follow-ups（提醒未来执行）

⚠️ **以下事项当前未做，等新流程稳定后再操作**：

### 1. 删除旧 Langfuse dataset（不可逆）

迁移完后这三个旧 dataset 仍保留在 Langfuse 上作为只读备份：

- `intention`（29 items）
- `recommend`（9 items）
- `replenish`（37 items）

**何时删**：
- 建议至少保留 **2-4 周**
- 期间确认新流程（A/B / DSPy / eval-online）在 `*-golden` / `*-online-temp` 上跑得稳定
- 确认没有业务代码或脚本还在引用旧 dataset 名

**怎么删**：
- Langfuse UI → Datasets → 选中 → Delete
- 或 CLI：当前没有 `eval-delete-dataset` 命令（known-issues #4：Langfuse 不支持 DELETE dataset API，只能逐条删 item）
- UI 操作最简单

⚠️ **删除前最后检查**：
```bash
# 确认本地 git 中没有引用旧 dataset 名
cd eval-ai-order
grep -rn "dataset: intention$\|dataset: recommend$\|dataset: replenish$" agents/ --include="*.yaml"
# 应该无输出（v2.1.0 改名后这些字段都带 -golden 后缀）
```

### 2. 阶段 6c 的多 dataset 并行实施（推迟）

当前 promptfoo_ab 只支持单 dataset。多 dataset 同时跑 + verdict 聚合留作 future work（`aggregate_verdicts` 已在 ab_verdict.py 中提前实现好，等真要做时直接 wire）。

触发条件：当业务真的有"CI 同时跑 golden + regression 才能 promote"的需求时再做。

### 3. Ingestion → OTel 迁移（长期技术债）

`submit_ingestion_batch` 当前用 deprecated 的 `/api/public/ingestion` 端点。Langfuse 未来真正删除时需要迁移到 `/api/public/otel/v1/traces`。无紧迫时间表，跟随 Langfuse 升级动作。
