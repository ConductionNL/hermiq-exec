# Tasks: agent-exec-handler (hermiq-exec / worker side)

Implemented 2026-07-07 (unit + static). Live AppAPI/jail e2e is deploy-gated —
see the notes on 4.1-4.5 below for exactly what is/isn't proven.

## 1. Registered shape expansion

- [x] 1.1 Expand `main.py::_agent_exec_task_type()` `input_shape` to the full
  contract field set: `correlation_id`, `agent_id`, `acting_user`, `model`,
  `system_prompt`, `prompt`, `skill_set`, `tool_allowlist`, `context_files`,
  `max_turns`, `timeout_seconds` (structured fields as JSON-in-TEXT).
- [x] 1.2 Expand `output_shape` to `status`, `response_text`, `artifacts`,
  `audit_json`, `error_detail`.
- [x] 1.3 Keep the provider/poll/workspace/self-recycle wiring in `main.py`
  unchanged — it is already correct.

## 2. Handler: run_agent_exec

- [x] 2.1 Replace the stub in `analyze.py::run_agent_exec` with a real handler that
  validates required inputs (`correlation_id`, `agent_id`, `acting_user`, `model`,
  `prompt`); missing/invalid → transport failure `(None, "<reason>")`.
- [x] 2.2 Validate `model` against an allowed Claude model set (`sonnet`/`opus`/
  `haiku`/`fable`); unknown → transport failure.
- [x] 2.3 Materialize `skill_set` (inlined `{slug, instructions}`) and
  `context_files` (`{filename: content}`) into the per-task scratch workspace. No
  network fetch to resolve skills. Filenames/slugs are sanitized
  (`_safe_name`) so a hostile payload cannot path-traverse out of the workspace.
- [x] 2.4 Invoke `claude -p` with `--model`, the assembled system/user prompt, the
  `tool_allowlist`-derived `--allowedTools` value, `--max-turns`, and
  `timeout_seconds` as the subprocess timeout (clamped to worker ceilings).
  NOTE (ambiguity, flagged for spec reconciliation): the installed CLI's
  `--allowedTools` grammar is its own tool-name syntax, not the fleet's
  `{appId}.{toolName}` id space; ids are passed through verbatim, which never
  *widens* the allowlist (an unrecognized id just never matches a real tool) but
  may under-enable fleet tools until an MCP-server id-mapping is specified.
- [x] 2.5 Build the OUTPUT envelope: map clean completion → `status: "success"`,
  model refusal → `refused`, non-zero/unusable → `failure`, timeout → `timeout`.
  Populate `response_text`, `artifacts`, `audit_json`
  (`model`/`turns`/`tool_calls`/`exit_code`/`duration_ms` plus `correlation_id`/
  `agent_id`/`acting_user` for attribution, per the security-boundary
  requirement to echo them), and redacted `error_detail`. NOTE (ambiguity):
  `--output-format json`'s single-result mode carries no per-tool-call detail
  (only `--output-format stream-json` does), so `audit_json.tool_calls` is
  always `[]` today; `refused` is a best-effort text heuristic
  (`_looks_like_refusal`) since the CLI exposes no machine-readable refusal
  signal — both flagged for spec reconciliation, not silently assumed.
- [x] 2.6 Return execution outcomes as `(output, None)`; return transport failures
  as `(None, error_message)` — matching `main.py`'s tuple-handler contract.

## 3. Security boundary (worker side)

- [x] 3.1 Never authenticate anywhere using `agent_id`/`acting_user`; echo them
  into `audit_json` only (verified: neither appears in the constructed CLI
  command — see `test_attribution_fields_never_used_to_authenticate`).
- [x] 3.2 Redact the Claude OAuth token and any jail env secret out of
  `response_text`, `audit_json`, `artifacts`, and logs; redact `error_detail`
  (`_redact`, pattern-based: `sk-ant-*`, `sk-*`, `Bearer <token>`, `oauth_token=*`).
- [x] 3.3 Never read a URL/host/target from the payload — no such field exists;
  rely solely on the jail's egress allowlist.
- [x] 3.4 Treat `prompt`/`context_files` as untrusted; never escalate the tool
  allowlist or egress based on their content (the allowlist is built once, from
  `tool_allowlist` only, before the CLI ever sees `prompt`/`context_files`).

## 4. Tests

- [x] 4.1 Unit: valid payload → success envelope with the exact output keys; the
  Claude CLI call is mocked (no live Anthropic call in CI). PROVEN (unit).
- [x] 4.2 Unit: missing required field / unknown model → transport failure
  `(None, msg)`. PROVEN (unit).
- [x] 4.3 Unit: timeout → `status: "timeout"`; refusal → `status: "refused"`;
  non-zero exit → `status: "failure"`. PROVEN (unit).
- [x] 4.4 Unit: `skill_set`/`context_files` are materialized into the workspace and
  no secret appears in the envelope. PROVEN (unit) for materialization + secret
  redaction. The post-job wipe itself is `main.py`'s existing, already-tested
  `_wipe()` — not re-tested here since this change doesn't touch it.
- [x] 4.5 Contract test: the consumed input keys/types and returned output
  keys/types match the field tables in this change's `design.md`
  (`test_contract_input_output_field_names`). Manual cross-check against
  `hermiq/openspec/changes/agent-exec-tasktype/design.md` confirms the two
  tables are identical as of 2026-07-07; an automated cross-repo diff is out of
  scope for this repo's test suite.

## 5. Deploy-gated (NOT implemented here — remains a TODO)

- [ ] 5.1 Live AppAPI/jail end-to-end run of a `hermiq:agent-exec` task against a
  running NC + AppAPI + `hermiq-exec` container with the real `claude` CLI and
  Anthropic egress. Requires a deployed instance; cannot be proven by unit tests.
- [ ] 5.2 Live verification that the `deploy/` iptables sidecar actually blocks
  egress to a host other than `api.anthropic.com` from inside a running
  `hermiq:agent-exec` job.
- [ ] 5.3 Live verification of skill/tool CLI-flag behavior against the exact
  `claude` CLI version the Dockerfile installs at build time (the flag mapping
  above was checked against the CLI installed in the verification environment,
  not the pinned Dockerfile version).
