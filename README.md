# Requirement Compiler

Compile `requirements.yaml` and reference images into a React + Express + SQLite application through 13 passes: requirements, database schema and obligations, backend APIs and tests, UI, and frontend triggers with end-to-end tests. Intermediate artifacts are saved under the output directory's `.arc/` folder.

## Environment

Install [Python 3.11+](https://www.python.org/downloads/), [Node.js 22.12+ with npm](https://nodejs.org/en/download), and [uv](https://docs.astral.sh/uv/getting-started/installation/). Ensure `node`, `npm`, and `uv` are available in your terminal; reopen it after installation.

Run from the repository root using your platform's shell.

**Windows (PowerShell)**

```powershell
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run playwright install chromium
```

**macOS / Linux (bash or zsh)**

```bash
uv sync
if [ ! -f .env ]; then cp .env.example .env; fi
uv run playwright install chromium
```

On supported Debian/Ubuntu Linux systems, use `uv run playwright install --with-deps chromium` instead to also install browser system dependencies; this may require sudo.

If npm needs to compile native dependencies such as `better-sqlite3`, install the platform's build tools:

| Platform | Build tools |
| --- | --- |
| Windows | Visual Studio Build Tools with **Desktop development with C++**, plus Python available to npm. |
| macOS | Xcode Command Line Tools: `xcode-select --install`. |
| Linux | Python, make, and a C++ compiler; on Debian/Ubuntu: `sudo apt-get install python3 build-essential`. |

Set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL` in `.env`. Use a model that accepts image input.

## Run

The input directory must contain `requirements.yaml` and its referenced images. The examples use `input/` and write the generated application to `outputs/demo/`. The commands below work in PowerShell, bash, and zsh.

### Full pipeline

```bash
uv run main.py input --output-dir outputs/demo
```

Add `--model-log` to display and save detailed model input/output, or `--port 33000` to change the backend port (default: 3000). Token usage and model latency are always shown.

Each stage appends a content-free usage record to `.arc/model_usage.jsonl`, even
without `--model-log`. Full pipeline runs also include a `model_usage` snapshot in
each stage of `.arc/compilation.json`. Use `run_id` to group a pipeline run and
`stage_run_id` to distinguish individual stage executions.

Records distinguish SDK calls, observed HTTP attempts (including SDK retries),
transport failures, proposal rejections, edit rejections, and acceptance failures.
Token sums include a report count; `null` means the provider did not report the
metric. Cached input is part of input, and reasoning is part of output: do not add
these subsets again. Request region sizes are UTF-8 bytes/characters, not tokens.
Usage records contain metrics and task labels, without prompt or source contents.

Compilation errors and `COMPLETED_WITH_FAILURES` return a nonzero exit status.

### Step by step

Run these commands in order. They call the same 13 passes with the same default settings as `main.py`. Step 01 uses `--no-browse` to match the non-interactive pipeline.

```bash
uv run 00_project_initialization.py --input input --output outputs/demo
uv run 01_explore_requirement.py --input input --output outputs/demo --no-browse
uv run 02_build_db_schema.py --input input --output outputs/demo
uv run 03_infer_db_obligations.py --input input --output outputs/demo
uv run 04_check_db_obligations.py --input input --output outputs/demo
uv run 05_implement_db.py --input input --output outputs/demo
uv run 06_design_backend_apis.py --input input --output outputs/demo
uv run 07_check_backend_apis.py --input input --output outputs/demo
uv run 08_implement_backend_tdd.py --input input --output outputs/demo
uv run 09_design_ui.py --input input --output outputs/demo
uv run 10_implement_ui.py --input input --output outputs/demo
uv run 11_design_frontend_triggers.py --input input --output outputs/demo
uv run 12_implement_frontend_tdd.py --input input --output outputs/demo
```

For individual steps, add `--pause` for teaching pauses or `--debug-model-io` for detailed model I/O. Set a custom backend port with `--port` in step 00.

### Start the generated application

```bash
cd outputs/demo
npm run db:init
npm run build
npm start
```

Open `http://localhost:3000` (or the port you selected).

## Local tests

```bash
uv run python -X utf8 -m unittest discover -s tests
```

These tests use local fixtures and mock model responses; they do not make live model requests.
