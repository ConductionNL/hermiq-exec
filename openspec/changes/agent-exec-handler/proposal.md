---
kind: code
depends_on: []
---

# Proposal: agent-exec-handler

## Why

`hermiq-exec` already registers the `hermiq:agent-exec` TaskProcessing task type
(`ex_app/lib/main.py`) but its handler is a deliberate stub
(`ex_app/lib/analyze.py::run_agent_exec`) that returns a structured
"not implemented" error, because — quoting the stub — "that hand-off does not
exist on the Hermiq side yet." That hand-off is now being ratified: ADR-032 move 5,
specified from Hermiq's (enqueuing) side in
`hermiq/openspec/changes/agent-exec-tasktype/`.

This change specifies the **worker (executing) side** of the *same* wire contract:
how the poll loop dispatches a `hermiq:agent-exec` task, invokes the Claude CLI
inside the egress jail, and constructs the result envelope Hermiq ingests. It
replaces the honest stub with a real handler, without changing the fleet boundary:
`hermiq-exec` still owns no agent engine, no skills catalog, no context system, and
no storage — it executes an *already-assembled* turn Hermiq ships it and reports
the result back.

## What Changes

- **`run_agent_exec` becomes a real handler** for the `hermiq:agent-exec` task
  type, consuming the input payload defined by the contract (`correlation_id`,
  `agent_id`, `acting_user`, `model`, `system_prompt`, `prompt`, `skill_set`,
  `tool_allowlist`, `context_files`, `max_turns`, `timeout_seconds`).
- **Claude-CLI execution model.** The handler materializes the payload into the
  per-task scratch workspace (skills as inlined files, `context_files` as
  read-only reference material) and invokes `claude -p` with the requested model,
  allowed tools, and turn/timeout bounds — the fleet-standard execution mechanism
  the `Dockerfile` installs. It runs inside `deploy/`'s egress jail; nothing in the
  payload can widen egress.
- **The result envelope** (`status`, `response_text`, `artifacts`, `audit_json`,
  `error_detail`) returned via `report_result`, distinguishing execution outcomes
  (`success`/`failure`/`timeout`/`refused`, returned as `output` with no
  `error_message`) from transport-level failures (bad input / CLI missing,
  returned as `report_result(error_message=...)`).
- **Security posture stated as requirements**: attribution-not-impersonation
  (`agent_id`/`acting_user` never used to authenticate anywhere), no secret
  leakage into results/logs, per-job workspace wipe (already in `main.py`), and
  bounded execution (timeout).
- **The existing wiring stays**: provider registration, poll loop, workspace
  lifecycle, and self-recycle in `main.py` are already correct — this change fills
  in the handler and expands the registered `input_shape`/`output_shape` to match
  the contract's field set.

## Cross-repo dependency

**Paired with `hermiq` change `agent-exec-tasktype`** (cross-repo — openspec
`depends_on` only resolves within one repo, so this is recorded in prose). The two
`design.md` files describe the identical wire contract and cross-reference each
other by repo/path. The task-type string (`hermiq:agent-exec`), input field
names/types, and output envelope MUST match verbatim. `hermiq-exec` is the *only*
side that registers the task type; Hermiq (PHP) is the consumer/scheduler.

## Non-Goals

- Owning any agent engine / tool-loop / capability profile / audit trail — those
  stay in Hermiq (PHP), per `openspec/project.md`'s fleet boundary.
- Persisting results — the worker returns them via `report_result`; storage is
  OpenRegister's (via Hermiq), per ADR-022.
- Context retrieval / RAG — the `prompt` arrives fully assembled from Hermiq's
  `ContextAssembler`.
- Replacing `doc-analyze` — `agent-exec` is a sibling task type, unrelated flow.
