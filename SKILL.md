---
name: agenttrace
description: Use when instrumenting AgentTrace, exporting task-production trajectories, capturing artifacts, tracking tokens, versioning files with git, or packaging redacted expert workflow evidence.
---

# AgentTrace

Use AgentTrace to capture agent execution traces plus the files and costs created during expert task production.

## Production Workflow

1. Start with a stable `session_id` for the task.
2. Capture input files and intermediate outputs with `TraceManager.capture_artifact(...)`.
3. Record token usage with `TraceManager.record_token_usage(...)`; traced model responses with common `usage` fields are recorded automatically.
4. Create git snapshots at meaningful production checkpoints with `agenttrace snapshot` or `GitVersionManager.snapshot(...)`.
5. Extract multi-version file trajectories with `GitVersionManager.extract_trajectory(...)`.
6. Before final packaging, generate a redacted preview with `agenttrace trajectory-preview`.
7. Show the preview to the expert and export only after they confirm the preview id with `agenttrace trajectory-export`.
8. Treat the exported zip as the deliverable. It contains `trajectory.json` plus redacted file copies under `files/redacted/`.

## Dynamic Redaction

Redaction is task-specific. Infer what must be hidden from the task context, filenames, headers, content, and generated outputs. Always redact hard secrets such as credentials, tokens, private URLs, emails, phone numbers, and account-like identifiers. Add semantic redaction for sensitive business entities, commercial terms, regulated fields, or personal data only when the current task makes them sensitive.

Keep useful task signals visible when they are not sensitive in context. Do not include raw sensitive values or placeholder maps in the final package.

## Commands

```bash
agenttrace snapshot --repo . --db traces.db --session-id task-001 --message "draft" path/to/file
agenttrace tokens --db traces.db --session-id task-001
agenttrace trajectory-preview --repo . --db traces.db --session-id task-001 --task-context "..." --output preview-dir path/to/file
agenttrace trajectory-export --repo . --db traces.db --session-id task-001 --task-context "..." --confirmed-preview-id <id> --output package.zip path/to/file
```

Preview and confirmed package layout:

```text
preview-dir/
  trajectory-preview.json
  files/redacted/latest/...
  files/redacted/versions/<commit-sha>/...

package-dir/
  trajectory.json
  files/redacted/latest/...
  files/redacted/versions/<commit-sha>/...
package.zip
```

## Guardrails

- Do not package raw artifacts directly.
- Do not export without expert confirmation of the redacted preview id.
- Regenerate preview approval when task context or files change.
- Keep trace databases, raw snapshots, and redaction internals out of source control unless explicitly approved.
