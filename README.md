# AgentTrace

AgentTrace is a local tracing and task-production trajectory toolkit for AI-agent and expert-in-the-loop workflows.

This fork keeps the original lightweight function tracing, evaluation records, SQLite storage, and dashboard workflow, and adds the production controls needed to package expert Codex task work safely:

- capture input files and intermediate artifacts
- record token usage for the task
- manage task file versions through git snapshots
- extract trajectories containing multiple versions of the same file
- dynamically redact sensitive information before final packaging
- require expert confirmation of the redacted preview before export

## Why This Version Exists

The original AgentTrace captured agent traces. Expert task production needs a wider provenance record: what files were used, what intermediate files were produced, how files changed across versions, and how much token budget was consumed.

This version treats the trace database and git history together as the task provenance layer. Final package export is blocked until a redacted preview has been generated and confirmed.

## Installation

Install from source:

```bash
git clone git@github.com:xiaotongWang0507/agenttrace.git
cd agenttrace
pip install -e .
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Core Concepts

`session_id`: stable id for one task production run. Use the same value for traces, artifacts, token usage, and trajectory export.

`artifact`: an input file or intermediate file captured into SQLite with path, hash, MIME type, size, snapshot content, metadata, and optional token usage.

`git snapshot`: a commit created at a meaningful production checkpoint, such as initial inputs, first draft, revised draft, final package candidate.

`trajectory`: exported provenance containing trace context, git metadata, token totals, and each selected file's versions across commits.

`redacted preview`: the export candidate after task-aware redaction. It must be shown to the expert before final packaging.

## Basic Tracing

```python
from agenttrace import TraceManager

tracer = TraceManager(db_path="traces.db")

@tracer.trace(tags=["agent"], session_id="task-001")
def run_step(user_input: str):
    return {"message": user_input}

run_step("hello")
```

When traced model responses contain common `usage` shapes such as `prompt_tokens`, `completion_tokens`, or `total_tokens`, AgentTrace records token usage automatically.

## Capture Inputs And Intermediate Artifacts

```python
from agenttrace import TraceManager

tracer = TraceManager(db_path="traces.db")

tracer.capture_artifact(
    "inputs/request.md",
    artifact_type="input",
    session_id="task-001",
    metadata={"source": "expert-upload"},
)

tracer.capture_artifact(
    "work/draft.md",
    artifact_type="intermediate",
    session_id="task-001",
)
```

Artifacts are stored in the `artifacts` SQLite table. The raw files remain local; the captured snapshot is used for provenance and downstream export.

## Record Token Usage

Manual token recording:

```python
tracer.record_token_usage(
    session_id="task-001",
    model="gpt-4.1",
    source="codex-interaction",
    prompt_tokens=2400,
    completion_tokens=900,
)
```

CLI summary:

```bash
agenttrace tokens --db traces.db --session-id task-001
```

The summary includes `prompt_tokens`, `completion_tokens`, `total_tokens`, per-model totals, and raw usage records.

## Manage File Versions With Git

Create snapshots during the expert workflow:

```bash
agenttrace snapshot \
  --repo . \
  --db traces.db \
  --session-id task-001 \
  --message "capture initial inputs" \
  inputs/request.md

agenttrace snapshot \
  --repo . \
  --db traces.db \
  --session-id task-001 \
  --message "capture revised draft" \
  work/draft.md
```

Python API:

```python
from agenttrace import GitVersionManager

versions = GitVersionManager(".", tracer=tracer, session_id="task-001")
versions.snapshot("capture final files", paths=["work/draft.md"])

trajectory = versions.extract_trajectory(paths=["work/draft.md"])
```

`extract_trajectory()` returns every committed version of the selected files, ordered from oldest to newest. Text files are included as UTF-8 content; binary files are represented as base64 when small enough, or omitted when above the size limit.

## Dynamic Redaction

Before packaging, AgentTrace builds a task-local redaction policy from:

- task context
- filenames
- file content
- table-like headers or nearby labels
- generated outputs

The redactor combines hard rules and semantic rules.

Hard rules redact secrets and private identifiers such as credentials, tokens, private URLs, emails, phone numbers, and account-like values.

Semantic rules redact task-sensitive business information only when the current task context indicates those fields are sensitive. The policy is generated per task and does not reuse placeholder maps across tasks.

The final package does not include raw sensitive values or the raw placeholder map.

## Preview Before Packaging

Generate a redacted preview:

```bash
agenttrace trajectory-preview \
  --repo . \
  --db traces.db \
  --session-id task-001 \
  --task-context "expert task production with private business inputs" \
  --output redacted-preview.json \
  inputs/request.md work/draft.md
```

Show `redacted-preview.json` to the expert. The preview contains an `approval.preview_id`.

Export is blocked without that confirmed preview id:

```bash
agenttrace trajectory-export \
  --repo . \
  --db traces.db \
  --session-id task-001 \
  --task-context "expert task production with private business inputs" \
  --confirmed-preview-id <preview_id> \
  --output task-001-trajectory.json \
  inputs/request.md work/draft.md
```

If task context or selected files change, generate a new preview and confirm the new preview id.

## CLI Reference

```bash
agenttrace start
agenttrace snapshot --repo . --db traces.db --session-id task-001 --message "checkpoint" path/to/file
agenttrace tokens --db traces.db --session-id task-001
agenttrace trajectory-preview --repo . --db traces.db --session-id task-001 --task-context "..." --output preview.json path/to/file
agenttrace trajectory-export --repo . --db traces.db --session-id task-001 --task-context "..." --confirmed-preview-id <id> --output package.json path/to/file
```

## Python API Reference

```python
from agenttrace import (
    DynamicRedactor,
    GitVersionManager,
    PackagingApprovalRequired,
    TraceManager,
    TrajectoryBuilder,
    TracerEval,
)
```

Important methods:

- `TraceManager.trace(...)`
- `TraceManager.capture_artifact(...)`
- `TraceManager.get_artifacts(...)`
- `TraceManager.record_token_usage(...)`
- `TraceManager.get_token_usage(...)`
- `GitVersionManager.snapshot(...)`
- `GitVersionManager.extract_trajectory(...)`
- `TrajectoryBuilder.preview_package(...)`
- `TrajectoryBuilder.export_package(...)`

## Evaluation Support

The original `TracerEval` flow is still available:

```python
from agenttrace import TracerEval

def data():
    return [{"input": "What is the capital of France?"}]

def scorer(output):
    return {"score": 1.0 if "paris" in output.lower() else 0.0}

scorer.name = "capital_checker"

evaluator = TracerEval(
    name="capital_eval",
    data=data,
    task=lambda question: "Paris",
    scores=[scorer],
)
```

## Dashboard

Start the local dashboard:

```bash
agenttrace start
```

For source installs, the frontend can also be started from `frontend/`:

```bash
cd frontend
npm run install:all
npm run start
```

## Safety Guardrails

- Do not commit `traces.db`, generated trace databases, `.DS_Store`, or `node_modules`.
- Do not place secrets in examples, tests, documentation, or committed trajectory packages.
- Do not export a package until the expert has reviewed the redacted preview.
- Do not include raw sensitive values, raw snapshots, or redaction placeholder maps in final packages.
- Regenerate preview approval when task context, selected files, or file content changes.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
