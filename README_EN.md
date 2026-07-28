[Chinese](./README.md) | **English**

# eval-shared

Shared Python toolkit for AI Agent evaluation: Langfuse CLI utilities, PromptFoo
rubric templates, and optional DSPy prompt optimization.

## Prerequisites

| Dependency | Required | Notes |
|------------|----------|-------|
| Python >= 3.11 | Required | Runs the CLI package |
| Langfuse | Required | Prompt management, datasets, traces, and scores |
| PromptFoo | Required | Installed with npm inside each evaluation project |
| LLM API | Required | Target model and judge model access |
| DSPy | Optional | Only needed for `eval-dspy-optimize` / `eval-dspy-pipeline` |

PromptFoo is still managed with npm in each business project. `eval-shared` is a
Python package installed with pip or uv.

## Installation

```bash
pip install eval-shared

# Optional DSPy support
pip install "eval-shared[dspy]"

# Local development
uv pip install -e ".[dev,dspy]"
```

## What This Package Provides

```text
eval-shared/
├── pyproject.toml
├── src/eval_shared/
│   ├── common/
│   │   ├── config.py
│   │   ├── langfuse_client.py
│   │   └── yaml_utils.py
│   ├── cli/
│   │   ├── sync_dataset.py
│   │   ├── sync_prompt.py
│   │   ├── eval_online.py
│   │   ├── export_dspy.py
│   │   ├── promote_prompt.py
│   │   ├── compare.py
│   │   ├── report.py
│   │   ├── promptfoo_ab.py
│   │   ├── dspy_pipeline.py
│   │   └── dataset_promote.py
│   └── dspy/
│       ├── loader.py
│       ├── module_factory.py
│       ├── metrics.py
│       ├── uploader.py
│       └── optimize.py
├── rubrics/
└── templates/
```

## CLI Commands

| Command | Purpose |
|---------|---------|
| `eval-sync-dataset` | Sync Langfuse Dataset items to/from local YAML |
| `eval-sync-prompt` | Sync Langfuse Prompt versions to/from local `prompt.yaml` |
| `eval-online` | Batch evaluate online observations and write Langfuse scores |
| `eval-export-dspy` | Export a Langfuse Dataset to DSPy example JSON |
| `eval-promote` | Move the `production` label to the current `staging` prompt version |
| `eval-report` | Summarize a PromptFoo JSON result file |
| `eval-compare` | Compare two PromptFoo JSON result files |
| `eval-promptfoo-ab` | Run PromptFoo A/B comparison for production vs staging |
| `eval-dspy-optimize` | Run DSPy optimization |
| `eval-dspy-pipeline` | Run DSPy optimization, A/B comparison, report, and Langfuse annotation |
| `eval-dataset-promote` | Promote items from `online-temp` into `golden` / `regression` datasets (regression writes are double-written to the local YAML SSOT with audit metadata and PII scrubbing) |

## Quick Start For A New Evaluation Project

```bash
mkdir eval-order && cd eval-order
git init
npm init -y
npm install --save-dev promptfoo
pip install eval-shared
```

Find the installed template directory:

```bash
TEMPLATES=$(python - <<'PY'
from pathlib import Path
import eval_shared

pkg_dir = Path(eval_shared.__file__).resolve().parent
for path in (pkg_dir.parent / "templates", pkg_dir.parent.parent / "templates"):
    if path.exists():
        print(path)
        break
else:
    raise SystemExit("templates directory not found")
PY
)
```

Copy the starter files:

```bash
cp "$TEMPLATES/.env.example" .env.example
cp "$TEMPLATES/.gitignore" .gitignore
cp .env.example .env

mkdir -p docs/eval-specs agents/intent-agent/datasets ci output
touch output/.gitkeep

cp "$TEMPLATES/promptfooconfig.template.yaml" agents/intent-agent/promptfooconfig.yaml
cp "$TEMPLATES/redteam.template.yaml" agents/intent-agent/redteam.yaml
```

Suggested npm scripts:

```json
{
  "scripts": {
    "test:agent": "promptfoo eval -c agents/$AGENT/promptfooconfig.yaml",
    "test:all": "for dir in agents/*/; do promptfoo eval -c ${dir}promptfooconfig.yaml; done",
    "view": "promptfoo view",
    "sync:dataset": "eval-sync-dataset",
    "sync:prompt": "eval-sync-prompt",
    "export:dspy": "eval-export-dspy",
    "promote": "eval-promote",
    "dspy:optimize": "eval-dspy-pipeline --config",
    "dspy:optimize:skip": "eval-dspy-pipeline --skip-optimize --config",
    "promptfoo:ab": "eval-promptfoo-ab",
    "cache:clear": "promptfoo cache clear"
  }
}
```

## Rubrics

The YAML files in `rubrics/` are design references. Do not use `$ref` to include
them directly in PromptFoo configs; current PromptFoo behavior can drop `type`
fields when expanding an array reference. Copy the needed assertions inline into
each project's `promptfooconfig.yaml`.

## Environment Variables

Use the four-block `.env` structure from `templates/.env.example`:

```bash
# Target model
DASHSCOPE_API_KEY=sk-xxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# PromptFoo judge model
PROMPTFOO_GRADING_MODEL=openai:chat:<grading-model-name>
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=sk-xxx

# Langfuse
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://your-langfuse.com
# LANGFUSE_BASE_URL is accepted as an alias; LANGFUSE_HOST wins when both are set
# LANGFUSE_SSL_VERIFY=false   # only for self-signed instances

# DSPy optimizer
DSPY_LM_MODEL=openai/<optimizer-model-name>
DSPY_LM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
DSPY_LM_API_KEY=sk-xxx
```

## Typical Workflow

```bash
# Pull staging prompt and run PromptFoo
eval-sync-prompt --agent intent-agent --label staging
AGENT=intent-agent npm run test:agent

# Compare production vs staging
eval-promptfoo-ab --agent intent-agent

# Promote when the A/B gate passes
eval-promote --agent intent-agent
```

`eval-promptfoo-ab` emits a three-state verdict with a regression-first veto:

- `A/B ❌` (WORSE): any regression (baseline PASS → candidate FAIL), or the pass
  rate drops beyond tolerance — one regression is enough to block.
- `A/B ✅` (BETTER): no regressions, at least one improvement, and the pass rate
  gain exceeds the tolerance threshold.
- `A/B 🟰` (SAME): everything else — changes within the tolerance band.

`eval-promote` refuses to move the `production` label while the staging version
carries an `A/B ❌` verdict; pass `--force` only after manually confirming the
failure is evaluation noise.
