# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

| 变更类型 | 版本号 | 示例 |
|----------|--------|------|
| 新增功能 | minor (`2.1.0`) | 新增 DSPy MIPROv2 优化 |
| 修复 bug | patch (`2.0.1`) | 修正 sync-dataset 分页 |
| 破坏性变更 | major (`3.0.0`) | 修改 CLI 参数接口 |

**原则**：只有 **≥ 2 个项目需要** 的规则才上推到本仓库。

---

## [Unreleased]

### Changed

- **#44 多轮观测判官只评当轮**（2026-07-29 拍板）：`template_vars.current_turn_view` 把多轮消息数组截为「system + 当轮 context 模板 + 当轮用户输入」（历史轮/assistant 全丢），`eval-online` 判官注入前自动应用——历史轮 rule_class/菜单陈旧导致的 S 族 374 条误判死根治。存量 16 条真实多轮 obs 回放 16/16 收敛（11 条消息→3 条、唯一 context 块）。测试 3 例。

### Fixed

- **🔴 eval-online 时间窗过滤从未生效（BUGFIXES #17）**：observations API 时间参数误用 `fromTimestamp`（scores API 的参数名），服务端静默忽略 → 每次都在拉全史（实测 totalItems 3255 vs 9）。改 `fromStartTime` + MockTransport 测试钉住。历史「窗口体量超预期/连续达 limit」怪象同源；判官重校准「n=2579 基线」因此作废（97% 为 04~06 旧流量）。

## [2.6.0] - 2026-07-29

> 语义澄清（#42④）：tag `v2.5.0` 指向 `efbf0ec`；其后 `24cb7ec`（子集结果数护栏测试 + README_EN 目录树补 dataset_promote.py，reviewer 核查门 P1/P2）为测试/文档补充，无行为变更、有意未 bump，随本版本一并发布。

### Added

- **#39 方案 A（契约 §2.3 regression vars 口径，Gate2 批 2026-07-29）**：新增 `common/template_vars.py` 通用解析器——observation 消息数组按 per-agent 映射配置（业务仓 `agents/<agent>/datasets/var-mapping.yaml`，本包不内置 agent 名）解析为 promptfoo 可直接消费的 dict 型模板变量；`eval-dataset-promote --to regression` 双写前自动解析，解析失败该条硬失败（不写半程）。id 改基于解析后 vars 复算；`--to golden` 保持原行为。reviewer 核查门修复随批：**多轮观测显式处置**（契约④：历史轮丢弃 + `multi_turn` metadata 标记 + 促迁清单人工过目）、`query_var ≠ query` 硬失败（PII v1 范围钉死防裸奔）、末条 user 为 `# Context Information` 模板时的兜底护栏。

### Fixed

- **promptfoo-ab item id 锚点**：四处 id 复算改为**存量 `id` 优先**（`_case_item_id`，与 sync push 同语义）——本地 vars 一旦经 PII 脱敏，hash 复算会偏离镜像 id，run-item 挂空（reviewer P1-1，机制缝提前闭合）。
- **sync-prompt pull 纯时间戳脏 diff**（#42③ 根治，08 期验收遗留）：内容/版本/标签均无变化时跳过写入，header 时间戳不再空转刷新；标签变化（如 staging 挪动）仍正常落盘；临时文件异常路径 try/finally 清理。

## [2.5.0] - 2026-07-28

### Fixed

- **🔴 `eval-promptfoo-ab` 陈旧结果文件当真（假 verdict 事故）**：PromptFoo 因环境问题（Node 23 vs `.nvmrc` 22 的 better-sqlite3 ABI 不匹配）中途崩溃时，`_run_promptfoo` 用 `Path(output).exists()` 判断"结果已生成"——上一轮的陈旧输出文件让判断恒真，整条 A/B 链在 07-27 的旧数据上得出假 "A/B ✅ 建议 promote"。修复三连：① 实跑前先 `unlink` 旧输出文件，`exists()` 恒等于"本次生成"；② 无论退出码，缺结果文件一律硬报错；③ 子集结果数 < 子集条数时硬报错（禁止把未跑 case 静默计为失败混入 verdict），合并层兜底路径补显式告警。测试 2 例钉住（陈旧文件清除 / telemetry 超时容忍仅限新文件）。
- **`_merge_hit_and_miss` 静默填充失败**：promptfoo 结果中匹配不到的 item 此前无声计 fail，现改为逐条告警（上游已有条数硬校验，此处为兜底可见性）。

### Changed

- **#8：`eval-dspy-pipeline` 决策建议改读结构化 verdict**：报告 Part 3 与终端最终建议不再字符串匹配 A/B 报告 markdown 文案（"🔴 回归"/"✅ **安全升级**"），统一读 `output/{agent}-ab-summary.json` 经 `verdict_from_ab_summary` 的三态枚举；SAME 获得独立分支（🟰 人工决策），文案矛盾时以结构化结论为准。测试 4 例。
- **去业务硬编码**：`sync_dataset` push 的 dataset 描述与 `metadata.source` 不再写死 `eval-ai-order`，改用运行目录名（`Path.cwd().name`）——本包不假设业务仓名。

### Added

- **CI（#20）**：`.github/workflows/ci.yml`——push/PR 触发，Python 3.11/3.12 矩阵，uv 安装 + pytest 门禁。
- **`uv.lock`（#20）**：锁定依赖解析入 git。
- **README_EN 三节对齐（#20）**：CLI Commands 补 `eval-dataset-promote`（11 条对齐）；Environment Variables 补 `LANGFUSE_BASE_URL` 双名兼容与 `LANGFUSE_SSL_VERIFY`；A/B 判定描述由旧"净改善"口径改为三态 verdict + 回归一票阻断 + promote 阻断门（与契约/AGENTS.md 一致）。

## [2.4.0] - 2026-07-28

### Added

- **regression 本地双写（契约 §2.3，#18 重轨提案拍板落地）**：`eval-dataset-promote --to regression` 写 Langfuse 的同时默认回写业务仓 `agents/{agent}/datasets/regression.yaml`（本地 YAML=SSOT，Langfuse 仅为运行镜像；`--no-local-write` 仅限演练）。本地条目携带 Langfuse item `id`（往返幂等锚点）+ 审计 metadata（promoted_from / promoted_from_item_id / promoted_at / promoted_reason）。`--to golden` 不自动回写，提示人工补 golden.yaml。
- **`common/pii.py` PII 脱敏 v1**：只作用于用户话术字段（messages `role=user` 的 content、`vars.query`）——手机号/连续 ≥7 位数字→`<PHONE>`、称呼式人名→`<NAME>`、桌号/会员号→`<ID>`；菜单/输出 JSON（food_id 等业务数字）与审计 metadata 不碰。工具输出逐处 diff 供人工过目。只管入 git 执行点；Judge 链路不脱敏（2026-07-28 拍板）。
- **`eval-sync-dataset` regression 无损往返**：pull 保留 item `id` 与审计 metadata（按 promoted_at 排序保证文件确定性、入 git 前脱敏、SSOT 文件头+覆盖警告）；push 优先使用条目自带 `id`（脱敏后 hash 漂移也能幂等覆盖）并带回审计 metadata——丢库可全量恢复。

### Changed

- `eval-sync-dataset` push 的 metadata 组装：条目自带 `metadata` 原样带回（source 可被覆盖），`assert`/`index` 仍以 YAML 为准；golden 链路行为不变。

## [2.3.1] - 2026-07-27

### Fixed

- **`eval-promptfoo-ab` 多行/JSON 型 vars 静默计零**：合并层按 `_vars_key(vars)` 匹配本地 promptfoo 结果，两类空白不一致导致全部匹配失败并计 0/9（实测 recommend A/B 两侧 0%，且把 0.0 分写回 Langfuse 污染缓存）：① YAML 折叠标量尾换行 vs promptfoo 返回值无；② YAML 折叠把换行折成 `}, {"` 而 promptfoo 链路紧凑化为 `},{"`。修复：`_normalize_var_value` 对字符串 strip、对 JSON 字符串解析后规范重序列化再参与匹配键；测试 3 例钉住。intention（单一 query var）不受影响，多 var agent（recommend/replenish）此前 A/B 结果均不可信。被污染的历史 ab-* run 分数用 `--no-cache` 重跑覆盖。

## [2.3.0] - 2026-07-26

### Added — 新增

- **`eval-online` 水位线（漏评保护，#13/#15 拍板）**：成功跑完后把启动时间写入 cwd `.eval-online-state.json`；下次运行若上次运行点早于 `--hours` 窗口起点，自动扩窗覆盖间隔。配套约定：dry-run、存在失败、或**达 `--limit` 上限（本批不完整，reviewer P1-4）**的运行均**不推进**水位线。适配「不上 cron、本地不定时手动跑」的触发方式——漏跑不再等于永久漏评。

### Fixed — Bug 修复

- **`eval-online` 静默截断改显式告警（#15）**：① 拉取量达 `--limit` 上限时告警"本批不完整"并入汇总（此前 42%/62% 等通过率只代表最新 50 条却无提示）；② rubric 注入超限截尾时计数并在汇总标注"评分可能失真"（verbose 模式逐条打印）。
- **`eval-online` 判官注入上限 8000 → 24000 字符，且支持评估器级 `maxChars` 覆盖**：告警上线后 96h 基线实测 **192/412 条**被 8000 截断（recommend/replenish input 含全菜单）——判官长期只看到半截数据，是历史低通过率不可信的主因之一。
- **`eval-promote` 标签剥离失效 + 静默假成功（#10/#21）**：Langfuse `newLabels` 只增/移动、不删除，旧"传过滤列表"写法静默无效。改为 **graveyard 移动方案**：promote 后回读 production 落点校验（不再信任 PATCH 返回值），把残留 A/B 状态标签移到最老的非本版本；仅一个版本时显式告警；剥离复核失败退出码 1。promote 成功后输出 Dify 同步契约提醒（PROTOCOL §2.3：production=Dify 实际运行版）。`langfuse_client` 新增 `list_prompt_meta`。
- **`eval-dataset-promote` list 型 input 幂等（#17）**：`compute_item_id` 支持 list（07-23 起 Dify obs input 均为 messages 数组）；此前 id=None 时 Langfuse 分配随机 id，重复 promote 产生重复 item。

### Docs — 文档

- `eval_online` 模块 docstring 真因口径修正（2026-07-26 对照实验：obs 级内置评估器只消费 OTel 通道数据，Dify 走经典 ingestion——本脚本是 Dify 线上打分唯一管道）。
- `templates/.env.example`：`LANGFUSE_SSL_VERIFY=false` 中间人风险标注（#23）。
- `AGENTS.md` promote 踩坑更新：graveyard 方案落地记录 + 测试 fake 必须还原"只增/移动"语义的教训。

## [2.2.0] - 2026-07-26

### Removed
- **退役一次性工具**（2026-07 现状评估 P3 预登记，用户 07-26 拍板）：
  - `eval-migrate-datasets-v2` CLI（`cli/migrate_datasets_v2.py` + `test_migrate_datasets_v2.py`）——三层 dataset 迁移已于 2026-05-11 执行完毕，07-24 Langfuse 全量重建后旧 `{agent}` dataset 已不存在，工具失去作用对象
  - `tests/spike_ingestion_dataset_run.py` 探针脚本（结论已沉淀在 `docs/dataset-run-migration.md` 与 eval-ai-order `known-issues.md`）
  - CLI 清单 12 → 11 条，契约 `PROTOCOL.md` 同步修订
- 后续一次性运维（临时查询/清理/探针）改用官方 `langfuse-cli`，不再新写 CLI 模块；边界见 `AGENTS.md`（langfuse-cli 只读/一次性，常规写操作仍走 `eval-sync-*` / promote）

## [2.1.3] - 2026-07-24

### Added — 新增

- **LICENSE**：补 MIT 许可证。仓库在 GitHub 已是公开状态（github.com/HaiYangBG1/eval-shared），此前无任何许可证声明，第三方法律上无权复用；2026-07-24 用户拍板保持公开并采用 MIT。
- **`pyproject.toml`**：补发布元数据 `readme` / `license` / `authors` / `classifiers` / `[project.urls]`（Homepage / Repository / Changelog）。

---

## [2.1.2] - 2026-07-23

### Fixed — Bug 修复

- **`common/config.py` / `common/langfuse_client.py`**：新增 `LANGFUSE_SSL_VERIFY=false` 支持（自签/IP 证书的私有部署）。修复 Langfuse 服务端强制 https 后 CLI 全线不可用的问题：http 请求被 302 跳转（POST 会被降级为 GET，不能靠 follow_redirects 解决），https 直连又因 IP 证书校验失败。`.env` 需将 URL 改为 `https://` 并设 `LANGFUSE_SSL_VERIFY=false`。该故障是线上评估 6 月起停摆的直接原因。
- **`templates/.env.example`**：补 `LANGFUSE_SSL_VERIFY` 说明。

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
