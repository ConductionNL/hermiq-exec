# agent-exec-handler (delta)

The worker side of ADR-032 move 5: executing a `hermiq:agent-exec` TaskProcessing
task. The identical wire contract, from the enqueuing side, is specified in
`hermiq/openspec/changes/agent-exec-tasktype/specs/agent-exec-tasktype/spec.md`.

## ADDED Requirements

### Requirement: Dispatch and execute a hermiq:agent-exec task

The worker MUST handle a `hermiq:agent-exec` task by materializing the input
payload into the per-task scratch workspace and executing the assembled turn via
the Claude CLI (`claude -p`) inside the egress jail. The worker MUST NOT perform
context retrieval, capability-profile enforcement, or storage of its own — it
executes the already-assembled `prompt` Hermiq shipped and reports the result.

#### Scenario: A valid agent-exec task runs via the Claude CLI

- **GIVEN** a `hermiq:agent-exec` task with `correlation_id`, `agent_id`, `acting_user`, `model`, and `prompt`
- **WHEN** the worker's poll loop dispatches it to the handler
- **THEN** the handler MUST invoke the Claude CLI with the requested `model` and assembled prompt inside the jail
- **AND** it MUST NOT retrieve additional context or read from any database

### Requirement: Materialize skills and context without network fetch

The worker MUST materialize `skill_set` (inlined `{slug, instructions}` entries)
and `context_files` (`{filename: content}`) into the per-task scratch workspace,
and MUST NOT perform any network fetch to resolve them (the egress jail forbids it).
The worker MUST restrict the run's tools to the `tool_allowlist` and MUST NOT widen
beyond it.

#### Scenario: Skills are materialized from inlined content

- **GIVEN** a task whose `skill_set` carries `[{"slug": "x", "instructions": "..."}]`
- **WHEN** the handler prepares the workspace
- **THEN** it MUST write the skill content into the workspace from the payload
- **AND** it MUST NOT fetch the skill over the network

#### Scenario: Tools are restricted to the allowlist

- **GIVEN** a task with `tool_allowlist` listing specific `{appId}.{toolName}` ids
- **WHEN** the handler invokes the CLI
- **THEN** only the listed tools MUST be enabled for the run
- **AND** no tool outside the allowlist MUST be enabled

### Requirement: Construct the result envelope

The worker MUST return an execution outcome as the task `output` with a `status` of
`success`, `failure`, `timeout`, or `refused`, alongside `response_text`,
optional `artifacts`, and `audit_json` capturing `model`, `turns`, `tool_calls`,
`exit_code`, and `duration_ms`. The worker MUST return a transport-level failure
(malformed/missing required input, unknown model, or missing CLI) as an
`error_message` with no output, matching the poll loop's tuple-handler contract.

#### Scenario: A clean completion returns a success envelope

- **GIVEN** a valid task whose Claude CLI run completes cleanly
- **WHEN** the handler builds the result
- **THEN** the `output` MUST carry `status: "success"`, `response_text`, and `audit_json`
- **AND** the reported `error_message` MUST be empty

#### Scenario: A timed-out run returns a timeout status

- **GIVEN** a valid task whose CLI run exceeds `timeout_seconds`
- **WHEN** the handler builds the result
- **THEN** the `output` MUST carry `status: "timeout"`
- **AND** `error_detail` MUST describe the timeout

#### Scenario: Malformed input is a transport failure

- **GIVEN** a task missing a required field or naming an unknown `model`
- **WHEN** the handler processes it
- **THEN** it MUST return `(None, error_message)` so the task is marked failed
- **AND** it MUST NOT invoke the Claude CLI

### Requirement: Attribution-not-impersonation and no secret leakage

The worker MUST treat `agent_id` and `acting_user` as audit attribution only —
echoing them into `audit_json` — and MUST NOT use them to authenticate to
Nextcloud or any fleet app. The worker MUST NOT emit the Claude OAuth token or any
jail environment secret into `response_text`, `audit_json`, `artifacts`, or logs,
and MUST redact `error_detail` before returning it.

#### Scenario: Attribution fields never authenticate

- **GIVEN** a task carrying `agent_id` and `acting_user`
- **WHEN** the handler executes
- **THEN** it MUST echo them into `audit_json`
- **AND** it MUST NOT authenticate to any service as `acting_user`

#### Scenario: No secret appears in the returned envelope

- **GIVEN** a completed run whose environment holds the Claude OAuth credential
- **WHEN** the handler builds the result envelope
- **THEN** no OAuth token or jail environment secret MUST appear in `response_text`, `audit_json`, `artifacts`, or `error_detail`

### Requirement: Bounded execution inside the jail

The worker MUST bound each run by `timeout_seconds` (clamped to its own ceiling)
and MUST rely solely on the `deploy/` egress jail for network control, reading no
URL, host, or network target from the payload. The per-task scratch workspace MUST
be wiped before and after each job so no skill file, context file, or CLI scratch
state survives into the next task.

#### Scenario: A run cannot widen its own egress

- **GIVEN** a task whose `prompt` or `context_files` attempts to direct the worker at an external host
- **WHEN** the handler executes
- **THEN** the worker MUST NOT act on any payload-supplied network target
- **AND** network egress MUST remain limited to the jail's allowlist
