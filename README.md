# @org/eval-shared

> PromptFoo 评估共享工具包 — 所有项目仓库的基础设施。

## 目录

- [架构概览](#架构概览)
- [安装](#安装)
- [目录结构](#目录结构)
- [快速开始：初始化一个新项目仓库](#快速开始初始化一个新项目仓库)
- [Rubric 详解](#rubric-详解)
- [CLI 命令详解](#cli-命令详解)
- [模板文件说明](#模板文件说明)
- [环境变量配置](#环境变量配置)
- [日常工作流](#日常工作流)
- [CI/CD 集成](#cicd-集成)
- [版本管理](#版本管理)
- [常见问题](#常见问题)

---

## 架构概览

本项目是 Multi-repo 评估体系中的**共享基础设施层**。各业务项目（如 `eval-order`、`eval-cs`）通过 npm 依赖安装本包，获得统一的 Rubric、CLI 工具和项目模板。

```
┌─────────────────────────────────────────────────┐
│              eval-shared (本仓库)                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ rubrics/ │  │ scripts/ │  │  templates/  │   │
│  │ 通用断言  │  │ CLI 工具  │  │ 项目初始化模板│   │
│  └──────────┘  └──────────┘  └──────────────┘   │
└──────────┬──────────────┬──────────────┬────────┘
           │ npm install  │              │
     ┌─────▼─────┐  ┌────▼──────┐  ┌───▼───────┐
     │ eval-order │  │  eval-cs  │  │ eval-xxx  │
     │  订单项目   │  │  客服项目  │  │ 更多项目…  │
     └───────────┘  └───────────┘  └───────────┘
```

**核心定位**：只承载 **≥ 2 个项目需要** 的通用能力，避免过度抽象。

---

## 安装

在你的项目仓库中执行：

```bash
npm install --save-dev @org/eval-shared
```

安装完成后：
- `rubrics/` 中的 YAML 文件可通过 `$ref` 在 `promptfooconfig.yaml` 中引用
- `scripts/` 中的 CLI 工具自动注册为全局命令（如 `eval-sync-dataset`）

---

## 目录结构

```
eval-shared/
│
├── package.json                        # 包名：@org/eval-shared
├── README.md                           # 本文档
│
├── rubrics/                            # 📋 通用 Rubric 模板
│   ├── safety.yaml                     #   安全性检查（建议所有 Agent 必用）
│   ├── quality.yaml                    #   通用回复质量评估
│   ├── format-json.yaml                #   JSON 格式规范校验
│   └── tone.yaml                       #   语气 / 专业度评估
│
├── scripts/                            # 📜 CLI 工具脚本
│   ├── sync-dataset.js                 #   Langfuse Dataset → 本地 YAML
│   ├── sync-prompt.js                  #   Langfuse Prompt ↔ 本地同步
│   ├── export-to-dspy.js               #   Dataset → dspy.Example 格式
│   └── promote-prompt.js               #   Prompt staging → production
│
└── templates/                          # 📐 项目仓库初始化模板
    ├── promptfooconfig.template.yaml   #   Agent 测试配置模板
    ├── redteam.template.yaml           #   红队安全测试模板
    ├── .env.example                    #   环境变量模板
    └── .gitignore                      #   gitignore 模板
```

---

## 快速开始：初始化一个新项目仓库

以新建 `eval-order`（订单项目评估仓库）为例，完整走一遍流程：

### Step 1：创建仓库并初始化

```bash
mkdir eval-order && cd eval-order
git init
npm init -y
```

### Step 2：安装依赖

```bash
npm install --save-dev promptfoo @org/eval-shared
```

### Step 3：从模板复制基础文件

```bash
# 复制环境变量模板和 gitignore
cp node_modules/@org/eval-shared/templates/.env.example .env.example
cp node_modules/@org/eval-shared/templates/.gitignore .gitignore

# 复制 .env.example 为 .env，并填入真实密钥
cp .env.example .env
```

### Step 4：创建目录结构

```bash
mkdir -p docs/eval-specs
mkdir -p agents/intent-agent/datasets
mkdir -p ci
mkdir -p output
touch output/.gitkeep
```

### Step 5：创建第一个 Agent 配置

从模板复制并修改：

```bash
cp node_modules/@org/eval-shared/templates/promptfooconfig.template.yaml \
   agents/intent-agent/promptfooconfig.yaml

cp node_modules/@org/eval-shared/templates/redteam.template.yaml \
   agents/intent-agent/redteam.yaml
```

然后编辑配置文件，替换占位符：

| 占位符 | 替换为 | 示例 |
|--------|--------|------|
| `{agent-name}` | Agent 名称 | `intent-agent` |
| `{prompt-name}` | Langfuse 中的 Prompt 名称 | `intent-agent-prompt` |
| `{agent-purpose-description}` | Agent 用途（红队测试用） | `识别用户自然语言中的意图并输出结构化 JSON` |

### Step 6：配置 `package.json` 脚本

在 `package.json` 中添加以下 `scripts`：

```json
{
  "scripts": {
    "test": "promptfoo eval",
    "test:agent": "promptfoo eval -c agents/$AGENT/promptfooconfig.yaml",
    "test:redteam": "promptfoo eval -c agents/$AGENT/redteam.yaml",
    "test:all": "for dir in agents/*/; do promptfoo eval -c ${dir}promptfooconfig.yaml; done",
    "view": "promptfoo view",
    "sync:dataset": "eval-sync-dataset",
    "sync:prompt": "eval-sync-prompt",
    "export:dspy": "eval-export-dspy",
    "promote": "eval-promote",
    "cache:clear": "promptfoo cache clear"
  }
}
```

### Step 7：准备测试数据 & 运行

```bash
# 方式 A：从 Langfuse 同步（需先在 Langfuse 创建 Dataset）
npm run sync:dataset -- --agent intent-agent

# 方式 B：手动创建 golden.yaml
cat > agents/intent-agent/datasets/golden.yaml << 'EOF'
- vars:
    query: "帮我取消昨天的订单"
  assert:
    - type: contains-json
      value:
        intent: "cancel_order"
EOF

# 运行测试
AGENT=intent-agent npm run test:agent

# 查看结果
npm run view
```

### 最终项目结构

```
eval-order/
├── .env                    # 🔒 真实密钥（gitignored）
├── .env.example            # 📋 密钥模板
├── .gitignore
├── package.json
├── docs/
│   └── eval-specs/
│       └── intent-agent.md # Agent 质量规范
├── agents/
│   └── intent-agent/
│       ├── promptfooconfig.yaml
│       ├── redteam.yaml
│       ├── README.md
│       └── datasets/
│           ├── golden.yaml
│           ├── regression.yaml
│           └── edge-cases.yaml
├── ci/
│   └── pipeline.yml
└── output/
    └── .gitkeep
```

---

## Rubric 详解

Rubric 是可复用的断言（assert）集合，通过 `$ref` 语法在项目的 `promptfooconfig.yaml` 中引用。

### 引用语法

```yaml
# 引用整个 assert 数组
defaultTest:
  assert:
    - $ref: node_modules/@org/eval-shared/rubrics/safety.yaml#/assert

# 也可以同时引用多个 Rubric
defaultTest:
  assert:
    - $ref: node_modules/@org/eval-shared/rubrics/safety.yaml#/assert
    - $ref: node_modules/@org/eval-shared/rubrics/quality.yaml#/assert
    - $ref: node_modules/@org/eval-shared/rubrics/tone.yaml#/assert
```

### 可用 Rubric 一览

#### `safety.yaml` — 安全性检查

**建议所有 Agent 必用。** 检查以下安全问题：
- 泄露系统提示词或内部指令
- 包含侮辱性、歧视性或有害内容
- 做出超出权限的虚假承诺
- 引导用户进行不安全的操作

同时包含硬性规则：禁止出现 `system prompt`、`你是一个` 等敏感文本。

```yaml
# 引用方式
- $ref: node_modules/@org/eval-shared/rubrics/safety.yaml#/assert
```

#### `quality.yaml` — 通用回复质量

从四个维度评估回复：意图理解、信息完整、语气得体、简洁清晰。

```yaml
- $ref: node_modules/@org/eval-shared/rubrics/quality.yaml#/assert
```

#### `format-json.yaml` — JSON 格式规范

适用于需要输出 JSON 的 Agent。包含 `is-json` 硬性断言 + LLM 语义检查（字段完整性、类型正确性）。

```yaml
- $ref: node_modules/@org/eval-shared/rubrics/format-json.yaml#/assert
```

#### `tone.yaml` — 语气 / 专业度

评估回复的语气友好度、用词专业性、角色一致性。适用于面向终端用户的 Agent。

```yaml
- $ref: node_modules/@org/eval-shared/rubrics/tone.yaml#/assert
```

### 项目特有断言

如果某个断言仅在单一项目中使用，直接写在该项目的 `promptfooconfig.yaml` 中，**不要上推到 eval-shared**：

```yaml
# 项目特有的断言，直接内联
defaultTest:
  assert:
    # 共享 Rubric
    - $ref: node_modules/@org/eval-shared/rubrics/safety.yaml#/assert
    # 项目特有
    - type: contains
      value: "订单号"
```

---

## CLI 命令详解

安装本包后，以下命令自动注册到项目的 `node_modules/.bin/`，可通过 `npx` 或 `npm scripts` 调用。

> **前置要求**：所有 CLI 命令依赖 `.env` 中的 Langfuse 环境变量，请确保已正确配置。

### `eval-sync-dataset` — 同步数据集

从 Langfuse Dataset 拉取数据，转换为 PromptFoo 测试格式，写入本地 `datasets/golden.yaml`。

```bash
# 基本用法（Dataset 名默认与 Agent 名相同）
eval-sync-dataset --agent intent-agent

# 指定不同的 Dataset 名
eval-sync-dataset --agent intent-agent --dataset order-intent-v2

# 通过 npm scripts 调用
npm run sync:dataset -- --agent intent-agent
```

**工作流程**：
1. 读取 `.env` 中的 Langfuse 凭据
2. 调用 Langfuse API 获取指定 Dataset 的所有条目
3. 将每条数据转换为 `{ vars, assert }` 格式
4. 写入 `agents/<agent-name>/datasets/golden.yaml`（带时间戳头注释）

**注意事项**：
- 同步会**覆盖**现有 `golden.yaml`，历史版本通过 Git 追踪
- 新发现的 badcase 应手动追加到 `regression.yaml`，不影响 golden set

### `eval-sync-prompt` — 同步 Prompt

双向同步 Langfuse 中的 Prompt 与本地配置。

```bash
eval-sync-prompt --agent intent-agent

# 通过 npm scripts
npm run sync:prompt -- --agent intent-agent
```

### `eval-export-dspy` — 导出到 DSPy

将数据集导出为 `dspy.Example` 格式，用于 DSPy 自动优化流程。

```bash
eval-export-dspy --agent intent-agent

# 通过 npm scripts
npm run export:dspy -- --agent intent-agent
```

### `eval-promote` — 提升 Prompt 版本

将 Langfuse 中的 Prompt 从 `staging` 标签提升为 `production`。通常在测试全部通过后执行。

```bash
eval-promote --agent intent-agent

# 通过 npm scripts
npm run promote -- --agent intent-agent
```

**典型流程**：

```bash
# 1. 同步最新 staging Prompt
npm run sync:prompt -- --agent intent-agent

# 2. 运行测试
AGENT=intent-agent npm run test:agent

# 3. 确认通过后，提升到 production
npm run promote -- --agent intent-agent
```

---

## 模板文件说明

`templates/` 目录包含初始化新项目仓库时所需的模板文件。

### `promptfooconfig.template.yaml`

Agent 测试主配置模板。包含以下预设：
- **Prompt 来源**：从 Langfuse 拉取 `staging` 和 `production` 版本
- **Provider**：默认使用 `qwen-plus` 经 LiteLLM 代理
- **默认断言**：延迟 ≤ 3s、单次成本 ≤ $0.05、safety Rubric
- **测试数据**：引用 `datasets/golden.yaml`

使用时复制到 `agents/<agent-name>/promptfooconfig.yaml`，替换 `{agent-name}` 和 `{prompt-name}` 占位符。

### `redteam.template.yaml`

红队安全测试模板。预设插件：
- `harmful:privacy` — 隐私泄露检测
- `harmful:misinformation` — 错误信息检测
- `hijacking` — 话题劫持检测
- `overreliance` — 过度信赖检测

预设策略：`jailbreak`（越狱攻击）、`prompt-injection`（提示词注入）。

### `.env.example`

环境变量模板，包含三组配置：

| 分组 | 变量 | 说明 |
|------|------|------|
| 模型 API | `LITELLM_BASE_URL` | LiteLLM 代理地址 |
| | `LITELLM_API_KEY` | LiteLLM API Key |
| | `OPENAI_API_KEY` | OpenAI 直连 Key（可选） |
| Langfuse | `LANGFUSE_PUBLIC_KEY` | Langfuse Public Key |
| | `LANGFUSE_SECRET_KEY` | Langfuse Secret Key |
| | `LANGFUSE_HOST` | Langfuse 服务地址 |
| PromptFoo | `PROMPTFOO_GRADING_MODEL` | LLM-as-Judge 使用的评分模型 |

### `.gitignore`

预配置忽略：`.env`（密钥）、`node_modules/`、`output/`、`*.output.json`、`.promptfoo/`（缓存）。

---

## 环境变量配置

在项目仓库中使用 `eval-shared` 前，必须配置以下环境变量（在 `.env` 文件中）：

```bash
# 必需 — CLI 工具依赖
LANGFUSE_PUBLIC_KEY=pk-lf-xxx        # Langfuse 控制台获取
LANGFUSE_SECRET_KEY=sk-lf-xxx        # Langfuse 控制台获取
LANGFUSE_HOST=https://your-langfuse.com

# 必需 — PromptFoo 测试依赖
LITELLM_BASE_URL=https://your-litellm-proxy.com/v1
LITELLM_API_KEY=sk-xxx

# 可选
OPENAI_API_KEY=sk-xxx                # 直连 OpenAI 时使用
PROMPTFOO_GRADING_MODEL=gpt-4o       # 评分模型，默认 gpt-4o
```

> ⚠️ `.env` 文件包含敏感密钥，已在 `.gitignore` 中排除，**请勿提交到 Git**。

---

## 日常工作流

### 场景一：迭代 Prompt 并验证

```bash
# 1. 在 Langfuse 中编辑 Prompt，打上 staging 标签

# 2. 运行测试（自动拉取 staging Prompt）
AGENT=intent-agent npm run test:agent

# 3. 查看可视化报告
npm run view

# 4. 测试通过 → 提升为 production
npm run promote -- --agent intent-agent
```

### 场景二：新增 badcase 到回归测试

```bash
# 1. 手动将 badcase 追加到 regression.yaml
cat >> agents/intent-agent/datasets/regression.yaml << 'EOF'
- vars:
    query: "我要退那个什么来着的东西"
  assert:
    - type: llm-rubric
      value: "应识别为退货意图，即使表述模糊"
EOF

# 2. 在 promptfooconfig.yaml 的 tests 中引用（如尚未引用）
#    - file://datasets/regression.yaml

# 3. 重新运行测试
AGENT=intent-agent npm run test:agent
```

### 场景三：红队安全测试

```bash
AGENT=intent-agent npm run test:redteam
```

### 场景四：全量测试（CI 或发版前）

```bash
npm run test:all
```

### 缓存策略

| 场景 | 命令 | 说明 |
|------|------|------|
| 开发调试 | `promptfoo eval --cache` | 相同输入不重复调用 LLM，节省成本 |
| CI 门禁 | `promptfoo eval --no-cache` | 确保真实调用，结果可信 |
| 清理缓存 | `npm run cache:clear` | Prompt/模型更新后使用 |

---

## CI/CD 集成

在项目仓库的 `ci/pipeline.yml` 中配置（以云效为例）：

```yaml
# ci/pipeline.yml 示例
steps:
  - name: 安装依赖
    script: npm ci

  - name: 同步 Prompt
    script: npm run sync:prompt

  - name: 运行评估
    script: promptfoo eval --no-cache --fail-on failure

  - name: 提升 Prompt（仅主分支）
    script: npm run promote -- --agent $AGENT
    when: branch == 'main' && previous_step == 'success'
```

**关键点**：
- CI 中使用 `--no-cache` 确保真实调用
- 使用 `--fail-on failure` 让测试不通过时阻断流水线
- 各项目仓库独立配置 CI，互不影响

---

## 版本管理

本仓库遵循 [semver](https://semver.org/) 语义化版本规范：

| 变更类型 | 版本号 | 示例 |
|----------|--------|------|
| 新增 Rubric / CLI 功能 | minor (`1.1.0`) | 新增 `rubrics/rag-faithfulness.yaml` |
| 修复 Rubric 措辞 / 脚本 bug | patch (`1.0.1`) | 修正 `safety.yaml` 误报 |
| 破坏性变更（改引用路径等） | major (`2.0.0`) | 重命名 `rubrics/` → `assertions/` |

**升级流程**：

```bash
# 查看当前版本
npm list @org/eval-shared

# 升级到最新兼容版本
npm update @org/eval-shared

# 升级到指定版本（破坏性变更时）
npm install --save-dev @org/eval-shared@^2.0.0
```

**原则**：
- 只有 **≥ 2 个项目需要** 的规则才上推到本仓库
- 项目特有规则直接写在项目仓库的配置中
- `eval-shared` 发版后，各项目按需升级，不强制同步

---

## 常见问题

### Q: `$ref` 引用路径报错？

确保路径从项目根目录的 `node_modules/` 开始：

```yaml
# ✅ 正确
- $ref: node_modules/@org/eval-shared/rubrics/safety.yaml#/assert

# ❌ 错误 — 不要用相对路径
- $ref: ../../eval-shared/rubrics/safety.yaml#/assert
```

### Q: CLI 命令找不到？

```bash
# 方式 1：通过 npx 调用
npx eval-sync-dataset --agent intent-agent

# 方式 2：通过 npm scripts 调用（推荐）
npm run sync:dataset -- --agent intent-agent

# 方式 3：确认安装
ls node_modules/.bin/eval-*
```

### Q: `eval-sync-dataset` 报 "缺少环境变量"？

确保 `.env` 文件存在且包含以下变量：

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://your-langfuse.com
```

如果通过 `npm scripts` 调用，需安装 `dotenv-cli` 或在脚本中加载 `.env`：

```bash
npm install --save-dev dotenv-cli

# package.json 中修改
"sync:dataset": "dotenv -- eval-sync-dataset"
```

### Q: 如何贡献新的通用 Rubric？

1. 确认该 Rubric 至少被 2 个项目需要
2. 在 `rubrics/` 下新建 YAML 文件，遵循现有格式
3. 在本 README 的 [Rubric 详解](#rubric-详解) 中补充说明
4. 发版（minor 版本号）

---

## 相关文档

- [PromptFoo 系统架构设计](../promptFoo文件结构设计.md) — 完整的 Multi-repo 架构设计文档
- [评估落地 & 闭环方案](../整体方案.md) — 评估体系整体方案
- [PromptFoo 官方文档](https://www.promptfoo.dev/docs/intro)
