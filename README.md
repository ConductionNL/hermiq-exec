# hermiq-exec

**Hardened, egress-jailed execution worker for Hermiq's LLM/agent jobs.**

hermiq-exec is a headless Nextcloud AppAPI ExApp: one long-running Python
container, registered as a Nextcloud **TaskProcessing** provider for two
custom task types, `hermiq:doc-analyze` and `hermiq:agent-exec`. Hermiq
(PHP, the Nextcloud app that owns Conduction's agent engine) enqueues tasks;
this worker polls for them, runs the heavy/untrusted part — document
analysis and, eventually, agent tool-loops — inside a network-isolated
container, and reports the result back. It has no user-facing UI.

This is the ExApp described in `SPECTR-NEXTCLOUD-PLAN.md` §6.5 (fleet
workspace root, `apps-extra/SPECTR-NEXTCLOUD-PLAN.md` — not part of this
repo, hence no relative link) and hydra ADR-050 — the piece that lets
Hermiq claim true ownership of Conduction's LLM/agent execution (plan §7)
without losing the network jail Specter's current Claude-CLI containers
already run under.

## Why this exists

Specter (repo `concurrentie-analyse`) already runs Claude Code CLI inside
network-jailed, fire-and-forget containers for document analysis — one
container per document batch, `--network` restricted to `api.anthropic.com`
+ Postgres via an iptables sidecar
(`docker-compose.intelligence.yml` + `Dockerfile.llm-worker`). That pattern
works, but it's bespoke: its own compose file, its own scheduling (a
DB-backed job queue), its own container lifecycle, entirely outside the
Conduction fleet's app model.

hermiq-exec is the same idea — Claude CLI in a network-jailed container —
re-expressed as a proper Nextcloud ExApp: AppAPI manages its lifecycle
(install/init/enable/heartbeat), Nextcloud's own **TaskProcessing** API
(`OCP\TaskProcessing`) is the job queue instead of a bespoke Postgres table,
and Hermiq's governance rails (kill-switch, approval gate, audit,
per-agent capability profile — plan §6.3) apply to every job it runs,
because Hermiq is what enqueues them.

Phasing (plan §6.5, decision #7): **the existing Specter docker-compose
jail keeps running until hermiq-exec reaches parity.** This repo does not
replace anything yet — it is the first, scaffolded step.

## Isolation delta — stated honestly

An AppAPI ExApp is **one long-running container per app**. There is no
per-task ephemeral spawn and no scale-to-zero — that's an AppAPI/docker-install
constraint, not a design choice made here. Specter's current jail spawns a
*fresh* container per document batch (`docker run --rm`), which bounds
blast radius about as tightly as a jail can: whatever state a compromised
run creates dies with the container.

hermiq-exec cannot do that. It is one persistent worker process handling
many jobs, one after another, for as long as the container lives. That is a
real, structural downgrade in isolation compared to Specter's per-batch
model, and it should not be described as equivalent.

Mitigations actually implemented in this scaffold (not just planned):

- **Per-job workspace wipe** — `ex_app/lib/main.py::_process_task` wipes
  the job's scratch directory (`$APP_PERSISTENT_STORAGE/scratch/{task_id}`)
  both *before* and *after* every task, so one job's document content or
  temp files cannot leak into the next job sharing the same process.
- **Self-recycle after N jobs** — the poll loop exits (`os._exit(0)`) once
  `HERMIQ_EXEC_MAX_JOBS` tasks have been processed, relying on the
  container's `restart: unless-stopped` policy to bring up a fresh process.
  This bounds how long any single process's accumulated state (memory,
  temp files the wipe missed, a wedged subprocess) can persist — it does
  not make each job as isolated as a fresh container, only less unbounded.
- **The network jail itself still bounds the blast radius** — even a fully
  compromised, long-lived worker process can only reach what
  `deploy/docker-compose.jail.yml`'s egress allowlist permits
  (`api.anthropic.com` + the Ollama host + the NC control channel).

If per-job container isolation is ever a hard requirement, that's a custom
deploy daemon outside AppAPI's model — explicitly out of scope for this
plan (§6.5) and not attempted here.

## What's here (scaffold status)

| Path | Status |
|---|---|
| `ex_app/lib/main.py` | Working skeleton: FastAPI + AppAPI lifecycle (`/heartbeat`, `/init`, `/enabled`), TaskProcessing provider registration, poll-worker loop, workspace wipe + self-recycle. Verified importable and runnable against the real `nc_py_api` 0.20 package (see "Verification" below). |
| `ex_app/lib/analyze.py` | `run_doc_analyze` (hermiq:doc-analyze): a working port of `concurrentie-analyse/scripts/analyze_batch.py`'s four extraction flows, minus the Postgres writer (persistence moves to Hermiq/OpenRegister — this worker returns structured output, it doesn't own storage, per ADR-022). Its Claude CLI subprocess call is a **marked integration point**: the invocation shape is real and tested (JSON parsing, retry, error handling all verified with the CLI mocked out), but it only produces a real answer inside the built image, with credentials + jail in place. `run_agent_exec` (hermiq:agent-exec) is a **deliberate stub** — see below. |
| `Dockerfile` | Working skeleton (python:3.11-slim + Node for the Claude CLI + non-root user). Not build-verified end-to-end in this environment — see "Verification". |
| `deploy/docker-compose.jail.yml` + `deploy/README.md` | Working skeleton for the egress jail + the `occ` registration steps. YAML-verified (`docker compose config`); not run against a live daemon. |
| `appinfo/info.xml` | Present so `deploy/README.md`'s `occ app_api:app:register --info-xml` step has something to point at. Not schema-validated against a live `info.xsd` — see the `<!-- OPEN ITEM -->` comment inside it about the (probably unnecessary) `<routes>` block. |
| `openspec/project.md` | Fleet-boundary + reference doc, same shape as the sibling `spectr` repo's. |

**`hermiq:agent-exec` is not implemented** — `run_agent_exec` always returns
a structured "not implemented" error rather than a fake result. Per plan §7,
Hermiq (PHP) is meant to own the agent tool-loop, the capability-profile
enforcement (§6.3), and the audit trail; this worker should only ever
receive an already-assembled prompt for the jailed-execution tail. That
hand-off doesn't exist on the Hermiq side yet. The task type + provider
*are* registered (so the shape is real and Hermiq can start dispatching to
it once it exists), but every task it currently receives will come back as
an honest failure, not a silent no-op success.

## Repo layout

```
hermiq-exec/
├── appinfo/info.xml              # AppAPI docker-install manifest
├── ex_app/lib/
│   ├── main.py                   # FastAPI + AppAPI lifecycle + poll worker
│   └── analyze.py                # task handlers (doc-analyze real, agent-exec stub)
├── Dockerfile                    # python:3.11-slim + Node/Claude CLI + non-root user
├── entrypoint.sh
├── requirements.txt
├── deploy/
│   ├── docker-compose.jail.yml   # --internal network + iptables sidecar
│   └── README.md                 # occ daemon:register / app:register steps
└── openspec/project.md
```

## Verification performed on this scaffold

- `python3 -m py_compile ex_app/lib/*.py` — passes.
- Real import + execution against the actual `nc_py_api[app]>=0.20.0`
  package (borrowed from the sibling `n8n-nextcloud`'s local `.venv`, which
  has it installed): `main.py` imports cleanly, the FastAPI route table is
  correct, and the `TaskProcessingProvider`/`TaskType` dataclasses for both
  task types serialize without error via the same `RootModel(...).model_dump()`
  call the SDK's own `register()` uses internally.
- `analyze.run_doc_analyze` exercised directly (short-document early exit,
  unknown-flow `ValueError`, Claude-CLI-unavailable `ValueError`, and a
  mocked successful-parse path) — all behave as documented. One of these
  runs incidentally invoked a real `claude` CLI call (it happened to be on
  `PATH` in the scaffolding environment) — a single low-cost `haiku` call
  on a throwaway prompt, not a deliberate integration test; later runs were
  mocked to avoid repeating that.
- `docker compose -f deploy/docker-compose.jail.yml config` — resolves
  cleanly, including the `network_mode: service:egress-gate` sharing and
  the external-network reference.
- **Not verified**: an actual `docker build .` (this sandbox has no route
  to any container registry) and anything requiring a live Nextcloud +
  AppAPI + Anthropic endpoint (provider registration, task polling,
  `occ` commands). Build and smoke-test both before first real use.

## License

EUPL-1.2, matching Hermiq and the rest of the Conduction fleet. See
[`LICENSE`](LICENSE).

## Repository status

This is a **new, standalone repository**, not yet pushed anywhere — the
Codeberg repo (`Conduction/hermiq-exec`) does not exist yet and needs to be
created before this can be pushed. Everything above is committed locally
only.
