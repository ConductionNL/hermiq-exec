# hermiq-exec — OpenSpec Project

hermiq-exec is the hardened, egress-jailed execution worker for Hermiq's
LLM/agent jobs (hydra ADR-050 §6.5, part of the Spectr-on-Nextcloud
re-platforming plan's Workstream D). It is a Nextcloud AppAPI ExApp: one
long-running Python container, registered as a **TaskProcessing** provider
for two custom task types (`hermiq:doc-analyze`, `hermiq:agent-exec`), that
Hermiq (PHP) enqueues work into and polls results back from.

This repo holds **one ExApp**: the FastAPI/nc_py_api worker, its
Dockerfile, and the egress-jail deploy topology. See the top-level
`README.md` for the full layout and honest scaffold-vs-stub status.

## Fleet boundary

| Concern | Owner | What it means for hermiq-exec |
|---|---|---|
| Agent engine, tool loop, capability profile, audit trail | **Hermiq (PHP)** | hermiq-exec never decides *what* to run or *who* is allowed to run it — it only executes what Hermiq enqueues and reports the result back. The `hermiq:agent-exec` task type is registered but stubbed until Hermiq's side of that hand-off exists (plan §7, §6.3). |
| Storage / persistence of results | **OpenRegister** (via Hermiq) | hermiq-exec returns structured output through the TaskProcessing `report_result` call; it does not write to any database itself. Specter's original `analyze_batch.py` wrote directly to Postgres — that responsibility does **not** carry over here (ADR-022: apps consume OR abstractions, they don't own bespoke storage). |
| Container lifecycle (install/init/enable/heartbeat) | **Nextcloud AppAPI** | hermiq-exec implements the lifecycle contract; it does not manage its own deployment, scaling, or networking beyond what `deploy/` documents. |
| Egress control | **Docker network topology**, not AppAPI | Verified fact (plan §6.5): AppAPI has no per-app egress knob. `deploy/docker-compose.jail.yml`'s `--internal` network + iptables sidecar *is* the control. |
| Heavy document analysis (interim) | **concurrentie-analyse** (Specter's existing jail) | `docker-compose.intelligence.yml` + `Dockerfile.llm-worker` keep running until hermiq-exec reaches parity (plan §6.5, decision #7). hermiq-exec does not replace that today. |

hermiq-exec does **not** own an agent engine, a skills catalog, a context
system, or a database. If a capability looks like it needs any of those,
that's a signal it belongs in Hermiq (PHP) or OpenRegister, not here.

## What does not move here

- **The agent tool-loop, MCP/tool allowlist enforcement, acting-user
  impersonation** — all stay in Hermiq per the ADR-001 amendment (plan §7).
  hermiq-exec receives an already-assembled prompt, nothing more.
- **Result persistence** — Hermiq/OpenRegister, not this worker.
- **Scheduling/enqueue logic** — Hermiq decides when and what to enqueue;
  hermiq-exec only polls and executes.
- **Specter's existing jailed containers** — stay in `concurrentie-analyse`
  as the interim path until hermiq-exec has parity; not touched by this
  repo or this change.

## Canonical references

- **`SPECTR-NEXTCLOUD-PLAN.md`** (fleet workspace root, `apps-extra/`) §6.5
  — the full design this repo scaffolds, including the isolation-delta
  analysis and the AppAPI/TaskProcessing facts it's built on.
- **hydra ADR-050** — Spectr on Nextcloud re-platform (proposed; drafted
  under `hydra/openspec/architecture/adr-050-spectr-market-intelligence-app.md`
  in the gate19 worktrees at the time this repo was scaffolded — not yet
  merged to hydra's `development` branch).
- **hydra ADR-022** — apps consume OpenRegister abstractions (hermiq-exec
  has no database of its own; results flow back through TaskProcessing to
  whatever OR-backed persistence Hermiq uses).
- **hermiq ADR-001** (to be amended per plan §7) — Hermiq owns agent
  execution, the tool loop, LLM providers; hermiq-exec is its jailed
  execution tail, not a second engine.
- **`concurrentie-analyse/docker-compose.intelligence.yml` +
  `Dockerfile.llm-worker` + `scripts/analyze_batch.py`** — the existing,
  production jail pattern this repo ports from and will eventually replace.

## Changes

None yet. This repo is itself the initial scaffold; per-capability OpenSpec
changes (starting with the `hermiq:doc-analyze` provider going from
scaffold to production-verified, then `hermiq:agent-exec` once Hermiq's
tool-loop hand-off exists) should land via `/opsx-new` once there is a live
Nextcloud + AppAPI instance to verify against.
