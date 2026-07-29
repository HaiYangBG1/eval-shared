# Bug 记录

针对 eval-shared 与《AI Agent 评估体系》规范的系统性审查记录。每条 bug 包含**症状 / 根因 / 修复**三段，方便回查。

修复版本：[v2.0.1](../CHANGELOG.md#201---2026-05-09)

## v2.1.0 重构（2026-05-11）

v2.1.0 不是单点修 bug，而是把"用 Langfuse Prompt label 当评估状态归档"这个**结构性误用**整体重构成 Dataset Run + 三态枚举。完整决策、API 摸底、踩坑、设计演进记录在独立日志：
[dataset-run-migration.md](./dataset-run-migration.md)

如果你看到这里是为了排查"为什么 production prompt 上有 `A/B ✅ 67.7%→...` 残留"——那是 v2.0 行为，v2.1.0 之后 promote 会自动剥离所有 A/B 状态 label。直接重新 promote 一次即可清理。

如果你看到一些行为跟 Langfuse OpenAPI spec 描述不一致（如 ingestion `body.id` 重复应该 upsert 但实际 first-write-wins），那是 v2.1.0 spike 时实测发现的灰色行为，记录在 migration log §坑 #4 / #8 / #9。

---

## 目录

- [一、代码 Bug（影响功能正确性）](#一代码-bug影响功能正确性)
  - [#1 DSPy LLM Judge 把内部属性当输入字段](#1-dspy-llm-judge-把内部属性当输入字段)
  - [#2 promote-prompt 标签管理把评估标签也剥掉了](#2-promote-prompt-标签管理把评估标签也剥掉了)
  - [#3 eval-online JSONPath 静默错误](#3-eval-online-jsonpath-静默错误)
  - [#4 DSPy 评估结果分数解析脆弱](#4-dspy-评估结果分数解析脆弱)
  - [#5 BOOLEAN 类型分数写回类型不匹配](#5-boolean-类型分数写回类型不匹配)
  - [#6 DSPy uploader 多 predictor 时 demos 丢失](#6-dspy-uploader-多-predictor-时-demos-丢失)
- [二、一致性与设计](#二一致性与设计)
  - [#7 模板命名不一致](#7-模板命名不一致)
  - [#8 文档残留 langfuse: 协议引用](#8-文档残留-langfuse-协议引用)
  - [#9 LANGFUSE_HOST 与 LANGFUSE_BASE_URL 双名未文档化](#9-langfuse_host-与-langfuse_base_url-双名未文档化)
  - [#10 npm scripts 文档缺项](#10-npm-scripts-文档缺项)
- [三、健壮性优化](#三健壮性优化)
  - [#11 promptfoo-ab 备份恢复逻辑过于侵入](#11-promptfoo-ab-备份恢复逻辑过于侵入)
  - [#12 LangfuseClient 不支持注入 http_client](#12-langfuseclient-不支持注入-http_client)
  - [#13 get_scores 增量评估时全表扫描](#13-get_scores-增量评估时全表扫描)
  - [#14 promote 缺少 A/B 失败阻断门](#14-promote-缺少-ab-失败阻断门)
  - [#15 promote 缺少 --force 兜底通道](#15-promote-缺少---force-兜底通道)

---

## 一、代码 Bug（影响功能正确性）

### #1 DSPy LLM Judge 把内部属性当输入字段

**文件**：`src/eval_shared/dspy/metrics.py`

**症状**：LLM Judge 模式下，评分提示词里出现 `_completed=...`、`_demos=...` 等 DSPy 内部字段，污染了评估上下文，导致评分模型理解任务不清。

**根因**：用 `dir(example)` 列举字段，把 DSPy `Example` 基类的内部属性也带了进来。

**修复**：改用 DSPy 官方 API `example.inputs().keys()`，仅取注册的输入字段；对老版本 DSPy 回退到 `_input_keys`。

```python
try:
    input_keys = list(example.inputs().keys())
except (AttributeError, TypeError):
    input_keys = list(getattr(example, "_input_keys", None) or [])
input_text = " | ".join(
    f"{f}={getattr(example, f, '')}" for f in input_keys
    if hasattr(example, f)
)
```

---

### #2 promote-prompt 标签管理把评估标签也剥掉了

**文件**：`src/eval_shared/cli/promote_prompt.py`

**症状**：`eval-promote` 推送 production 时，pipeline 此前打的 `A/B ✅ 70.0%→80.0%` 等审计标签会一起被清掉。

**根因**：旧实现是 `if label in (...): drop`，但用了「黑名单」式判断且条件不全；同时把 `latest` 这种 Langfuse 自带标签也粗暴剥离。

**修复**：换成保留白名单 + 反向剥离。

```python
_RESERVED_LABELS = {"latest", "production", "staging"}

labels = [lb for lb in existing_labels if lb not in _RESERVED_LABELS]
labels.append("production")
```

Langfuse 会自动维护 `latest`，无需手工保留。

---

### #3 eval-online JSONPath 静默错误

**文件**：`src/eval_shared/cli/eval_online.py`

**症状**：用户写了 `$.choices[*].text` 或 `$..content` 这类 JSONPath，CLI 没有报错但取到的值是空字符串，看起来像是线上没数据。

**根因**：内置实现只支持 `$.a.b.c` 简单点路径，遇到不支持的语法没有显式拒绝。

**修复**：在解析前显式拒绝 `[*]`、`?(`、`..`、`@.`，给出清晰 ValueError，引导用户改写或自己实现。

---

### #4 DSPy 评估结果分数解析脆弱

**文件**：`src/eval_shared/dspy/optimize.py`

**症状**：DSPy 升级后 `Evaluate` 返回的不再是裸 float，而是 `EvaluationResult` 对象；老代码 `float(result) > 1` 立刻抛 TypeError，整个流水线挂掉。

**根因**：直接对返回值做 `float()`，没考虑 API 演进。

**修复**：

```python
raw = getattr(result, "score", result)
try:
    score_val = float(raw)
except (TypeError, ValueError):
    click.echo(f"  ⚠️ 无法解析评估结果：{raw!r}，回退为 0.0", err=True)
    score_val = 0.0
score = score_val / 100.0 if score_val > 1.0 else score_val
```

兼容新旧 API，且无法解析时不挂流水线，仅告警 + 回退 0.0。

---

### #5 BOOLEAN 类型分数写回类型不匹配

**文件**：`src/eval_shared/cli/eval_online.py`

**症状**：在 Langfuse Score Config 里把 `dataType` 设为 `BOOLEAN` 时，写回 0.7 / 0.83 这种 float，Langfuse API 422。

**根因**：Langfuse BOOLEAN 类型只接受整数 0/1。

**修复**：

```python
write_value = int(round(score)) if score_type == "BOOLEAN" else score
```

---

### #6 DSPy uploader 多 predictor 时 demos 丢失

**文件**：`src/eval_shared/dspy/uploader.py`

**症状**：用 `chain_of_thought` 模块时，DSPy 优化后的 demos 只取到一份；上传到 Langfuse 后线上 prompt 缺少推理链 few-shot。

**根因**：`for name, predictor in module.named_predictors()` 循环内有 `break`，只取第一个 predictor 就退出。

**修复**：移除 `break`。指令（instruction）取第一个非空，demos 跨所有 predictor 累积。

```python
for name, predictor in module.named_predictors():
    sig_instructions = getattr(predictor.signature, "instructions", None)
    if sig_instructions and not instructions:
        instructions = sig_instructions
    demos.extend(getattr(predictor, "demos", []))
```

---

## 二、一致性与设计

### #7 模板命名不一致

**文件**：`templates/`

`dspy-optimize.example.yaml` 与同目录下的 `promptfooconfig.template.yaml`、`redteam.template.yaml` 命名不统一。

**修复**：重命名为 `dspy-optimize.template.yaml`，同步 README 模板清单。

---

### #8 文档残留 langfuse: 协议引用

**文件**：`AI底座/评估体系/AI Agent 评估体系.md` §6.1

文档里仍举例 `prompts: [langfuse:intent-agent-prompt:staging]`，但 PromptFoo 早就不支持这个协议。新人复制粘贴会得到 404 错误。

**修复**：统一改写为 `file://prompt.yaml`，并附 `eval-sync-prompt` 拉取流程。

---

### #9 LANGFUSE_HOST 与 LANGFUSE_BASE_URL 双名未文档化

**文件**：`AI底座/评估体系/AI Agent 评估体系.md` §5.4

代码里 `LANGFUSE_HOST` 与 `LANGFUSE_BASE_URL` 二选一都行，但文档只说了一个，业务项目复制 .env 时容易踩坑。

**修复**：在 §5.4 注明 `# 兼容 LANGFUSE_BASE_URL，HOST 优先`。

---

### #10 npm scripts 文档缺项

**文件**：`AI底座/评估体系/AI Agent 评估体系.md` §5.5

文档示例里漏了 `test`、`export:dspy`、`promote` 三个常用脚本，新建项目时容易漏。

**修复**：补全脚本清单，与 README 快速开始对齐。

---

## 三、健壮性优化

### #11 promptfoo-ab 备份恢复逻辑过于侵入

**文件**：`src/eval_shared/cli/promptfoo_ab.py`

**症状**：A/B 跑流程会先备份 `agents/<agent>/prompt.yaml`、覆盖、再恢复，一旦中途异常本地 prompt 文件就处于不一致状态。

**根因**：作者当时不知道 PromptFoo 支持 `-p file://...` 直接覆盖 config。

**修复**：改用 PromptFoo CLI 的 `-p file://{abs_prompt}` 标志，写入 `output/{agent}-ab-{baseline,candidate}.prompt.yaml` 临时文件，主流程不再触碰 `agents/`。步骤从 4 步（备份/拉取/跑/恢复）降到 3 步。

---

### #12 LangfuseClient 不支持注入 http_client

**文件**：`src/eval_shared/common/langfuse_client.py`

**症状**：单元测试要 mock HTTP 必须 monkeypatch `httpx.Client`，写起来繁琐；多个 CLI 命令也无法共享同一个 client。

**修复**：

```python
def __init__(self, *, config=None, http_client: httpx.Client | None = None):
    ...
    if http_client is not None:
        self._client = http_client
        self._owns_client = False
    else:
        self._client = httpx.Client(...)
        self._owns_client = True

def close(self) -> None:
    if self._owns_client:
        self._client.close()
```

`close()` 仅关闭自有 client，避免共享 client 被意外关闭。

---

### #13 get_scores 增量评估时全表扫描

**文件**：`src/eval_shared/common/langfuse_client.py`

**症状**：`eval-online --hours 24` 实际拉的是项目下所有 score，再在客户端按时间过滤，数据量大时直接超时。

**修复**：`get_scores` 新增 `from_timestamp: str | None = None`，映射到 Langfuse 的 `fromTimestamp` query 参数；`eval_online.py` 调用时传 `from_timestamp=since`。

---

### #14 promote 缺少 A/B 失败阻断门

**文件**：`src/eval_shared/cli/promote_prompt.py`

**症状**：A/B 评估流水线打了 `A/B ❌ 67.7%→16.1% 回归17` 标签后，eval-promote 仍能毫无阻碍地把这个版本推上生产。

**修复**：promote 前扫描标签，发现 `A/B ❌` 前缀则 `ClickException` 退出。

```python
failed_ab = [lb for lb in existing_labels if lb.startswith("A/B ❌")]
if failed_ab and not force:
    raise click.ClickException(
        f"该版本带有 A/B 失败标签：{failed_ab}。如确认推送请加 --force。"
    )
```

---

### #15 promote 缺少 --force 兜底通道

**文件**：`src/eval_shared/cli/promote_prompt.py`

**症状**：A/B 失败但人工已确认是评估噪音（如 latency 抖动）时，没有兜底通道强推。

**修复**：与 #14 配套，新增 `--force` flag，绕过 `A/B ❌` 阻断门。

```python
@click.option("--force", is_flag=True, help="跳过 A/B ❌ 失败标签的阻断门")
def main(agent: str, dry_run: bool, force: bool):
    ...
```

测试 `tests/test_promote_prompt.py::test_promote_force_bypasses_ab_failure_gate` 覆盖该路径。

---

### #16 promptfoo-ab 把上一轮的陈旧结果文件当本次结果（假 verdict 事故）

**文件**：`src/eval_shared/cli/promptfoo_ab.py`

**症状**（2026-07-28 实锤）：shell 里的 Node 是 v23，而仓库 `.nvmrc` 要求 22——better-sqlite3 ABI 不匹配导致 PromptFoo 启动即崩（`ERR_DLOPEN_FAILED`），没有写出任何新结果。但 `output/{agent}-ab-*.json` 里还留着 07-27 的旧文件，`_run_promptfoo` 用 `Path(output_path).exists()` 判断"结果已生成"恒真，于是整条 A/B 链在昨天的数据上继续算分：合并层把本地新增却在旧结果中不存在的 case 静默计为失败，最终产出一份数字自洽、结论完整的**假报告**（甚至给出"A/B ✅ 建议 promote"）。连续三轮"迭代→重跑"全部在回放旧数据，迭代师完全无感。

**根因**：三层防线全部缺位——① 实跑前不清理旧输出文件，`exists()` 区分不了"本次生成"与"上次残留"；② 非零退出码被"telemetry 超时"的容忍逻辑一律放行；③ 合并层对"promptfoo 结果数 < 请求 case 数"没有任何校验，缺的 case 静默计 fail。

**修复**（v2.5.0）：① `_run_promptfoo` 实跑前 `unlink` 旧输出，此后 `exists()` 恒等于"本次生成"；② 缺结果文件时无论退出码一律 `ClickException`；③ `_run_promptfoo_subset` 对结果数硬校验（少于子集数即报错并提示排查 node / better-sqlite3 ABI）；④ `_merge_hit_and_miss` 兜底路径逐条告警不再静默。测试 2 例钉住（陈旧文件必须被清除 / telemetry 容忍仅限新文件）。

**教训**：「证据即运行」不仅要求跑过，还要求证据文件与本次运行强绑定。凡是"跑完读文件"的链路，跑前清理旧产物应是标配。

### #17 eval-online 时间窗参数名错误——窗口过滤从未生效（拉全史）

**症状**（2026-07-29 实锤）：`eval-online --hours 12` 拉回的 observation 中 97% 是 2026-04~06 的旧流量（判死 1180 条里 1146 条 obs startTime 在 4-6 月）；历史上「48h 窗自然流量体量超预期、连续多轮达 limit 上限」的怪象同源。判官重校准清窗轮据此产出的「n=2579 基线」实为三个月旧流量重评读数，基线口径作废重拍。

**根因**：`langfuse_client.get_observations` 给 `/api/public/observations` 传的时间过滤参数名是 `fromTimestamp`，但该 API 认的是 **`fromStartTime`**（`fromTimestamp` 是 scores API 的参数）。自托管 Langfuse 对未知参数**静默忽略**，查询退化为无时间过滤拉全史。实测同一查询：`fromTimestamp` → totalItems=3255（全史）；`fromStartTime` → totalItems=9（真实窗口内）。曾被误归因为水位线 gap-extension（该机制需要 `.eval-online-state.json` 存在，实际该文件因从未有完整批次而不存在）。

**修复**（2026-07-29）：参数名改 `fromStartTime`，注释钉死两个 API 的参数差异；测试 1 例用 MockTransport 断言必须传 `fromStartTime` 且不得传 `fromTimestamp`。

**教训**：对「静默忽略未知参数」的 API，窗口/过滤类参数上线时必须做一次**边界实测**（窗口内外各取样本核对返回量），不能只看请求成功。
