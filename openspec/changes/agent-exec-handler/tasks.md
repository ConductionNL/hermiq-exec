# Tasks: agent-exec-handler (hermiq-exec / worker side)

Spec-first change. These tasks are the future implementation checklist for the
worker side of the `hermiq:agent-exec` hand-off. No code is written in this change.

## 1. Registered shape expansion

- [ ] 1.1 Expand `main.py::_agent_exec_task_type()` `input_shape` to the full
  contract field set: `correlation_id`, `agent_id`, `acting_user`, `model`,
  `system_prompt`, `prompt`, `skill_set`, `tool_allowlist`, `context_files`,
  `max_turns`, `timeout_seconds` (structured fields as JSON-in-TEXT).
- [ ] 1.2 Expand `output_shape` to `status`, `response_text`, `artifacts`,
  `audit_json`, `error_detail`.
- [ ] 1.3 Keep the provider/poll/workspace/self-recycle wiring in `main.py`
  unchanged — it is already correct.

## 2. Handler: run_agent_exec

- [ ] 2.1 Replace the stub in `analyze.py::run_agent_exec` with a real handler that
  validates required inputs (`correlation_id`, `agent_id`, `acting_user`, `model`,
  `prompt`); missing/invalid → transport failure `(None, "<reason>")`.
- [ ] 2.2 Validate `model` against an allowed Claude model set; unknown → transport
  failure.
- [ ] 2.3 Materialize `skill_set` (inlined `{slug, instructions}`) and
  `context_files` (`{filename: content}`) into the per-task scratch workspace. No
  network fetch to resolve skills.
- [ ] 2.4 Invoke `claude -p` with `--model`, the assembled system/user prompt, the
  `tool_allowlist`-derived allowed-tools set, `--max-turns`, and `timeout_seconds`
  as the subprocess timeout (clamped to worker ceilings).
- [ ] 2.5 Build the OUTPUT envelope: map clean completion → `status: "success"`,
  model refusal → `refused`, non-zero/unusable → `failure`, timeout → `timeout`.
  Populate `response_text`, `artifacts`, `audit_json`
  (`model`/`turns`/`tool_calls`/`exit_code`/`duration_ms`), and redacted
  `error_detail`.
- [ ] 2.6 Return execution outcomes as `(output, None)`; return transport failures
  as `(None, error_message)` — matching `main.py`'s tuple-handler contract.

## 3. Security boundary (worker side)

- [ ] 3.1 Never authenticate anywhere using `agent_id`/`acting_user`; echo them
  into `audit_json` only.
- [ ] 3.2 Redact the Claude OAuth token and any jail env secret out of
  `response_text`, `audit_json`, `artifacts`, and logs; redact `error_detail`.
- [ ] 3.3 Never read a URL/host/target from the payload — no such field exists;
  rely solely on the jail's egress allowlist.
- [ ] 3.4 Treat `prompt`/`context_files` as untrusted; never escalate the tool
  allowlist or egress based on their content.

## 4. Tests

- [ ] 4.1 Unit: valid payload → success envelope with the exact output keys; the
  Claude CLI call is mocked (no live Anthropic call in CI).
- [ ] 4.2 Unit: missing required field / unknown model → transport failure
  `(None, msg)`.
- [ ] 4.3 Unit: timeout → `status: "timeout"`; refusal → `status: "refused"`;
  non-zero exit → `status: "failure"`.
- [ ] 4.4 Unit: `skill_set`/`context_files` are materialized into the workspace and
  cleaned by `main.py`'s wipe; no secret appears in the envelope.
- [ ] 4.5 Contract test: the consumed input keys/types and returned output
  keys/types match the field tables in BOTH this change's and
  `hermiq/.../agent-exec-tasktype`'s `design.md`.
