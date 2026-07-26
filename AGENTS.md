# AGENTS.md — eval-shared

> 给 AI 编程助手的项目指南。人类用户请看 [README.md](./README.md) / [README_EN.md](./README_EN.md)。

## 项目定位

Multi-repo 评估体系的 **共享基础设施层**（Python 包，v2.0 从 npm 包迁移而来）。下游业务项目（`eval-ai-order`、`eval-cs`、…）通过 pip 或相邻目录安装本包，获得统一的 CLI 工具、Rubric 模板、DSPy 优化能力。

**核心原则**：只承载 **≥ 2 个项目需要** 的通用能力，不为单个项目特殊化。新增功能前先确认存在第二个使用方。

## 仓库结构

```
src/eval_shared/
├── common/          # Langfuse REST 客户端、.env 加载、yaml 工具
├── cli/             # 所有 eval-* CLI 命令的入口模块
└── dspy/            # DSPy MIPROv2 优化（loader / module_factory / metrics / uploader / optimize）
rubrics/             # 通用 Rubric 模板（设计参考，不能 $ref，需内联到业务项目）
templates/           # 业务项目脚手架：promptfooconfig / redteam / dspy-optimize / .env / .gitignore
tests/               # pytest 测试
```

CLI 入口在 `pyproject.toml [project.scripts]`（11 条）：`eval-sync-dataset`、`eval-sync-prompt`、`eval-online`、`eval-export-dspy`、`eval-promote`、`eval-report`、`eval-compare`、`eval-promptfoo-ab`、`eval-dspy-optimize`、`eval-dspy-pipeline`、`eval-dataset-promote`。

> **一次性运维不再新写 CLI 模块**（v2.2.0 起，`eval-migrate-datasets-v2` 已退役示范）：临时查询 / 清理 / 探针类操作改用官方 `npx langfuse-cli api <resource> <action>`（自托管实例需 `LANGFUSE_HOST` 指向实例地址；证书自签时需 `NODE_TLS_REJECT_UNAUTHORIZED=0`，安全账同 `LANGFUSE_SSL_VERIFY`）。**边界**：langfuse-cli 只许查询/一次性运维；prompt / dataset 的常规写操作一律走本仓 `eval-sync-*` / `eval-promote` / `eval-dataset-promote`（命名约定、标签流、幂等 ID 只在这里实现）。

## 环境与运行

```bash
# 开发模式安装（含 DSPy + 测试依赖）
uv pip install -e ".[dev,dspy]"
# 或：pip install -e ".[dev,dspy]"

# 运行测试
pytest

# 跑单个 CLI（需要 .env 配齐 Langfuse）
eval-sync-dataset --agent <agent> --direction pull
```

下游业务项目（如 `eval-ai-order`）通常作为相邻目录使用本包：业务项目里的 `scripts/eval-shared.js` 以 `python -m eval_shared.cli.<module>` 调用，**不通过 pip 安装到下游 venv**。所以 CLI 模块必须可用 `python -m` 跑通。

## 关键设计约定

| 约定 | 规则 |
|------|------|
| **Single Source 原则** | DSPy 任务描述从 `description_file: agents/*/prompt.yaml` 读取；LLM Judge 评分规则从 `rubric_file: docs/eval-specs/*.md` 读取。**不要在 dspy-optimize.yaml 里内联重复内容。** |
| **Rubric 复用方式** | `rubrics/*.yaml` 只是设计参考，业务项目要把断言**内联**进 `promptfooconfig.yaml`。**不能用 `$ref`**——PromptFoo 展开数组后会丢 `type` 字段。 |
| **PromptFoo 归属** | PromptFoo 仍由业务项目的 npm 安装，本包不依赖。CLI 只通过 `subprocess` 或文件交互。 |
| **Langfuse 客户端** | 统一走 `common/langfuse_client.py`，CLI 不要直接 `httpx` Langfuse API。`LangfuseClient.__init__` 支持 `http_client` 注入用于测试。 |
| **环境变量双名兼容** | `LANGFUSE_HOST` 与 `LANGFUSE_BASE_URL` 都接受，`HOST` 优先。文档新增示例时两者都要提。 |
| **升级判定策略** | **回归优先，一票阻断**：任何回归（baseline PASS → candidate FAIL）→ ❌ WORSE；无回归、有改善、通过率提升 > tolerance → ✅ BETTER；其余 → 🟰 SAME。`safe_to_upgrade` 等价于 verdict == BETTER（见 `common/ab_verdict.py`）。latency 等基础设施噪音应通过断言 `weight: 0` 排除，而不是靠判定口径放宽。 |
| **promote 阻断门** | `eval-promote` 检测到 `A/B ❌` 标签会拒绝；人工确认是噪音时用 `--force` 兜底。 |

## 模型分层（写文档/示例时必须保持一致）

```
能力梯度：被测模型 ＜ 评分模型 ≈ 线上评估模型 ≤ DSPy 优化器
成本梯度：被测模型（最低） → DSPy 优化器（最高）
```

- 被测：成本优先，生产大量调用
- 评分 / 线上评估：判断力优先，能力须 > 被测
- DSPy 优化器：作为「教师」，能力须 ≥ 评分模型

## 高频踩坑（修过的真实 bug，改动相关代码时注意）

**DSPy 模块**
- `metrics.py`：用 `example.inputs().keys()` 取输入字段，**不要用 `dir(example)`**——会带进 DSPy 内部属性污染评分上下文（BUGFIXES #1）
- `uploader.py`：`for predictor in module.named_predictors():` **不要 break**，`chain_of_thought` 有多个 predictor，demos 需跨所有 predictor 累积；instructions 取第一个非空（#6）
- `uploader.py`：上传 staging prompt 时**必须自动追加 `{"role": "user", "content": "{{query}}"}`**，否则 PromptFoo A/B 拉到 staging 后模型收不到用户输入，全量回退 default（#1 in eval-ai-order known-issues）
- `optimize.py`：DSPy `Evaluate` 返回值可能是 `EvaluationResult` 也可能是 float，先 `getattr(result, "score", result)` 再 try/except `float()`（#4）
- DSPy demo 必须以 JSON 格式上传，过滤 `augmented` 等元数据噪音

**eval-online**
- JSONPath 只支持 `$.a.b.c` 点路径；遇到 `[*]`、`?(`、`..`、`@.` 必须显式 `ValueError` 而不是返回空串（#3）
- BOOLEAN 类型的 Score Config 写回必须 `int(round(score))`，传 float 会 422（#5）
- `get_scores` 增量评估传 `from_timestamp`（映射 Langfuse `fromTimestamp` 参数），不要客户端过滤全表（#13）

**promote_prompt**
- Langfuse `PATCH …/versions/{v}` 的 `newLabels` 语义是**只增/移动、不删除**（2026-07-24 实测）——不要再写"传过滤后列表来剥离标签"的代码，那是静默无效的（2026-07-26 已修）
- 剥离 A/B 状态标签走 **graveyard 移动方案**（2026-07-26 落地）：promote 后回读 production 落点校验，再把残留 A/B 标签移到最老的非本版本；只有一个版本时显式告警。staging 标签无法删除，留待下次 sync push 自然移走。测试 fake 必须还原"只增/移动"语义，否则会对剥离生效产生假阳性（旧测试的教训）
- A/B ❌ 阻断门 + `--force` 兜底是配套的，改一个要同时改另一个（#14、#15）

**promptfoo_ab**
- 用 `promptfoo eval -p file://{abs_prompt}` 把临时 prompt 写到 `output/{agent}-ab-*.prompt.yaml`，**不要去备份/覆盖/恢复 `agents/<agent>/prompt.yaml`**（#11）

**Langfuse Dataset**
- `get_dataset_items` 必须分页，旧版只拉前 100 条会静默截断（已修，新增类似拉取要保持分页）
- upsert 传 `null` 不会清除已有 `expectedOutput`，文档要标注

完整 bug 上下文见 [docs/BUGFIXES.md](./docs/BUGFIXES.md)。

## 模板维护

`templates/` 文件命名统一用 `*.template.yaml`（不是 `*.example.yaml`）。新增模板时同步更新 README 模板清单和 `pyproject.toml` 的 `force-include`。

## 详细文档

| 文档 | 用途 |
|------|------|
| [README.md](./README.md) | 完整使用说明、CLI 参数、工作流场景 |
| [docs/BUGFIXES.md](./docs/BUGFIXES.md) | 历史 bug 的「症状/根因/修复」详细记录 |
| [CHANGELOG.md](./CHANGELOG.md) | 版本变更、v1→v2 迁移 |
