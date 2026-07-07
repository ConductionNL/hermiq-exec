# Design: agent-exec-handler (hermiq-exec / worker side)

This is the authoritative design for ADR-032 move 5 from the **worker's
(executing) side**. Hermiq's (enqueuing) side of the *same* wire contract lives in
`hermiq/openspec/changes/agent-exec-tasktype/design.md`. The two documents describe
one contract: the task-type string (`hermiq:agent-exec`), every input field
name/type, and every output field name/type are identical in both. This document
never contradicts that one; it only adds the worker's execution detail.

## Context

Shipped, do-not-contradict reality this integrates with:

- `ex_app/lib/main.py` already registers the `hermiq:agent-exec` task type and
  provider on `/enabled`, runs the `next_task` poll loop, dispatches by task type,
  wipes the per-task scratch workspace before and after each job, and self-recycles
  after N jobs. The dispatch table already routes `hermiq:agent-exec` to a "tuple"
  handler (`run_agent_exec`) that returns `(output, error_message)`.
- `ex_app/lib/analyze.py` holds the working `run_doc_analyze` (the pattern to
  follow for Claude-CLI invocation, JSON parsing, retries, and the untrusted-input
  posture) and the current `run_agent_exec` STUB this change replaces.
- `deploy/` is the egress jail: an `--internal` Docker network plus an iptables
  sidecar allowlisting only `api.anthropic.com`. AppAPI has no per-app egress knob
  (verified, `openspec/project.md`) — the jail *is* the control.
- `Dockerfile` (python:3.11-slim + Claude CLI) provides the `claude` binary and,
  at deploy time, the read-only-mounted OAuth credential.

## Fleet boundary (unchanged)

The worker executes an **already-assembled** turn and reports the result. It does
not decide *what* to run or *who* may run it, does not assemble context, does not
enforce the capability profile (Hermiq already applied it when building the
payload), and does not persist anything. `agent_id`/`acting_user` are audit
attribution the worker echoes back in `audit_json` — never credentials it acts on.

## Execution backend

The backend is the **Claude CLI** (`claude -p` with OAuth), per the fleet rule and
`Dockerfile`. The `model` field selects the Claude model (`--model`). This mirrors
`run_doc_analyze`'s `_call_claude` shape, generalised to carry the assembled
`system_prompt`/`prompt`, the allowed tools, and the turn/timeout bounds.

## The wire contract — INPUT (`hermiq:agent-exec` task `input`)

Identical to `hermiq/.../agent-exec-tasktype/design.md`. Structured values arrive
as JSON-encoded `TEXT`. The worker's responsibility per field:

| field | type | req | worker responsibility |
|---|---|---|---|
| `correlation_id` | TEXT | yes | Echo verbatim into `audit_json` and treat as the opaque correlation key. Never parsed for meaning. |
| `agent_id` | TEXT | yes | Attribution only — echo into `audit_json`. Never used to authenticate anywhere. |
| `acting_user` | TEXT | yes | Attribution only — echo into `audit_json`. The worker holds no session/credential for this user and MUST NOT authenticate to Nextcloud or any app as them. |
| `model` | TEXT | yes | Pass to `claude --model`. Validate against an allowed set; reject unknown models as a transport failure. |
| `system_prompt` | TEXT | no | Provide as the CLI's system prompt / persona context. |
| `prompt` | TEXT | yes | The fully-assembled turn. Passed to `claude -p`. Treated as data + instructions Hermiq already prepared; the worker does no retrieval. |
| `skill_set` | TEXT (JSON array) | no | `[{"slug","instructions"}]`. Materialize each into the scratch workspace as a skill file the CLI can pick up. Inlined content only — the worker performs NO network fetch to resolve skills (the jail forbids it). |
| `tool_allowlist` | TEXT (JSON array) | no | `{appId}.{toolName}` ids → the CLI's allowed-tools set. Empty/absent = no tools enabled. The worker never widens beyond this list. |
| `context_files` | TEXT (JSON object) | no | `{filename: content}` written read-only into the scratch workspace as reference material. |
| `max_turns` | NUMBER | no | `claude --max-turns`, clamped to the worker's ceiling. |
| `timeout_seconds` | NUMBER | no | Hard wall-clock budget for the CLI subprocess, clamped to the worker's ceiling. On expiry the run is a `timeout` outcome. |

## The wire contract — OUTPUT (`hermiq:agent-exec` task `output`) + error channel

Identical to Hermiq's side. Two channels:

- **Execution outcome** (the CLI ran and produced a verdict, including refusal or
  timeout): return the `output` dict below and `report_result(error_message=None)`.
- **Transport-level failure** (cannot run the task at all: malformed/missing
  required input, unknown model, `claude` binary absent, jail failure): return
  `(None, "<reason>")` so `main.py` calls `report_result(error_message=...)` and
  the task is marked failed. This is the existing "tuple handler" contract in
  `main.py`.

| field | type | meaning |
|---|---|---|
| `status` | TEXT | `success` \| `failure` \| `timeout` \| `refused`. `failure` = CLI ran but errored (non-zero exit, unusable output); `refused` = the model declined. |
| `response_text` | TEXT | The CLI's final response text (stdout, trimmed). |
| `artifacts` | TEXT (JSON array) | Optional `[{"name","content"}]` produced files the run wants to hand back. |
| `audit_json` | TEXT (JSON object) | `{ "model", "turns", "tool_calls": [{"tool","ok"}], "exit_code", "duration_ms" }` — captured execution metadata for Hermiq's ADR-041 audit trail. |
| `error_detail` | TEXT | Human-readable detail when `status != "success"`, already redacted worker-side. |

## Security boundary (spec'd as requirements)

1. **Egress jail.** The handler runs inside `deploy/`'s jail; it MUST NOT read any
   URL/host/target from the payload and act on it — there is no such field. All
   network egress is the iptables allowlist (Anthropic only).
2. **Attribution, not impersonation.** `agent_id`/`acting_user` are echoed into
   `audit_json` and never used to authenticate. The only credential in the jail is
   the read-only Claude OAuth, scoped to Anthropic.
3. **No secret leakage.** The handler MUST NOT emit the Claude OAuth token or any
   jail environment secret into `response_text`, `audit_json`, `artifacts`, or
   logs. `error_detail` MUST be redacted before it leaves the worker.
4. **Untrusted content.** `prompt`/`context_files` may contain injection attempts
   (like `doc-analyze`'s `document_text`); the handler treats them as data and
   never escalates the tool allowlist or egress in response to their content.
5. **Bounded + wiped.** Execution is bounded by `timeout_seconds`; `main.py`
   already wipes the scratch workspace before and after every job, so no skill
   file, context file, or CLI scratch state survives into the next task.

## In-flight cancellation (known constraint)

NC TaskProcessing has no push-cancel channel to a running worker (flagged in both
designs). The worker's contribution to the kill-switch story is (a) honouring
`timeout_seconds` as a hard bound, and (b) returning a clean envelope so Hermiq can
decide, on ingest, whether to drop a late result by `correlation_id`. The worker
does not itself consult the kill-switch — that stays on Hermiq's side.

## Registered shape expansion

`main.py`'s `_agent_exec_task_type()` currently declares only `agent_id` + `prompt`
(input) and `response_text` (output). This change expands the registered
`input_shape`/`output_shape` to the full contract field set above, so the task type
NC advertises matches what the handler consumes and returns.

## Open implementation questions (for tasks.md, not blocking the contract)

- Exact CLI flag mapping for `tool_allowlist` and `skill_set` materialization
  (workspace layout the installed `claude` version expects). The wire contract is
  fixed regardless of CLI-flag detail.
- Whether `artifacts` are collected from a known workspace output dir or must be
  named by the run. Contract carries them as `[{name, content}]` either way.
