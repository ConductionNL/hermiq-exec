"""hermiq-exec ExApp — hardened, egress-jailed execution worker for Hermiq.

Headless AppAPI TaskProcessing PROVIDER: registers two custom task types
(hermiq:doc-analyze, hermiq:agent-exec), then polls Nextcloud for tasks that
Hermiq (PHP) enqueues, executes them inside this container's egress jail
(see deploy/), and reports results back. There is no browser-facing UI — the
FastAPI app only implements the AppAPI lifecycle contract
(/heartbeat, /init, /enabled) plus nothing else.

See SPECTR-NEXTCLOUD-PLAN.md §6.5 (hydra ADR-050) for the full design and
openspec/project.md for the fleet boundary. Packaging pattern (FastAPI +
nc_py_api + AppAPIAuthMiddleware + lifespan) follows the sibling ExApp
n8n-nextcloud/ex_app/lib/main.py; the task-processing poll-worker loop and
the egress jail are new to this app.

VERIFIED against the live checkout at nextcloud-docker-dev/workspace/server
(NC 33.0.0-dev):
  - `nc.providers.task_processing.register/next_task/report_result` exist
    and match core/Controller/TaskProcessingApiController.php's wire shapes.
  - `next_task_batch` (nc_py_api's batch poll helper) has **no** server route
    in this checkout — grep of TaskProcessingApiController.php found no
    `next_batch` endpoint, only the singular `/tasks_provider/next`. nc_py_api
    0.20's docstring claims "Available starting with Nextcloud 33" but this
    33.0.0-dev checkout does not have it yet. Default to single-task polling
    (`next_task`); batch polling is opt-in via HERMIQ_EXEC_USE_BATCH_POLL and
    will silently return nothing if the route is absent (nc_py_api swallows
    the 404 as a caught NextcloudException).
"""

import asyncio
import logging
import os
import shutil
import typing
from contextlib import asynccontextmanager
from pathlib import Path

from analyze import run_agent_exec, run_doc_analyze
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import (
    nc_app,
    persistent_storage,
    run_app,
    setup_nextcloud_logging,
)
from nc_py_api.ex_app.integration_fastapi import AppAPIAuthMiddleware
from nc_py_api.ex_app.providers.task_processing import (
    ShapeDescriptor,
    ShapeEnumValue,
    ShapeType,
    TaskProcessingProvider,
    TaskType,
)

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="[%(funcName)s]: %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("hermiq_exec")
LOGGER.setLevel(logging.DEBUG)


# ── Configuration ───────────────────────────────────────────────────
APP_ID = os.environ.get("APP_ID", "hermiq_exec")

TASK_TYPE_DOC_ANALYZE = "hermiq:doc-analyze"
TASK_TYPE_AGENT_EXEC = "hermiq:agent-exec"
PROVIDER_ID_DOC_ANALYZE = f"{APP_ID}_doc_analyze"
PROVIDER_ID_AGENT_EXEC = f"{APP_ID}_agent_exec"

# Self-recycle + poll tuning (see appinfo/info.xml environment-variables for
# the admin-facing description of each).
MAX_JOBS_BEFORE_RECYCLE = int(os.environ.get("HERMIQ_EXEC_MAX_JOBS", "200"))
POLL_INTERVAL_SECONDS = float(os.environ.get("HERMIQ_EXEC_POLL_INTERVAL", "5"))
POLL_BATCH_SIZE = int(os.environ.get("HERMIQ_EXEC_POLL_BATCH", "1"))
USE_BATCH_POLL = os.environ.get("HERMIQ_EXEC_USE_BATCH_POLL", "false").lower() == "true"

# Handler dispatch table: task type id -> (kind, callable).
# "sync" handlers return an output dict directly (raise ValueError on bad
# input); "tuple" handlers return (output, error_message) themselves — used
# by the deliberately-stubbed hermiq:agent-exec.
_SYNC_HANDLERS = {TASK_TYPE_DOC_ANALYZE: run_doc_analyze}
_TUPLE_HANDLERS = {TASK_TYPE_AGENT_EXEC: run_agent_exec}

_jobs_processed = 0
_poll_task: asyncio.Task | None = None
_poll_should_run = False


# ── TaskProcessing provider + task-type definitions ─────────────────
def _doc_analyze_task_type() -> TaskType:
    return TaskType(
        id=TASK_TYPE_DOC_ANALYZE,
        name="Hermiq: Document Analysis",
        description=(
            "Extract structured requirements/features from a tender document, "
            "external source, competitor page, or scientific paper, using the "
            "Claude CLI inside hermiq-exec's egress jail."
        ),
        input_shape=[
            ShapeDescriptor(
                name="flow",
                description="Which extraction flow to run.",
                shape_type=ShapeType.ENUM,
            ),
            ShapeDescriptor(
                name="document_text",
                description="Raw document/page content to analyze (untrusted).",
                shape_type=ShapeType.TEXT,
            ),
        ],
        output_shape=[
            ShapeDescriptor(
                name="result_json",
                description="Extraction result as a JSON string (array or object, flow-dependent).",
                shape_type=ShapeType.TEXT,
            ),
            ShapeDescriptor(
                name="items_extracted",
                description="Count of extracted items, for quick triage without parsing result_json.",
                shape_type=ShapeType.NUMBER,
            ),
        ],
    )


def _agent_exec_task_type() -> TaskType:
    return TaskType(
        id=TASK_TYPE_AGENT_EXEC,
        name="Hermiq: Agent Execution",
        description=(
            "Run an already-assembled agent prompt/tool-loop under the "
            "hermiq-exec egress jail. STUB: not implemented yet — see "
            "ex_app/lib/analyze.py::run_agent_exec."
        ),
        input_shape=[
            ShapeDescriptor(
                name="agent_id",
                description="Hermiq Agent object UUID (acting identity for the audit trail).",
                shape_type=ShapeType.TEXT,
            ),
            ShapeDescriptor(
                name="prompt",
                description="Assembled prompt/context (Hermiq ContextAssembler output, plan §6.4).",
                shape_type=ShapeType.TEXT,
            ),
        ],
        output_shape=[
            ShapeDescriptor(
                name="response_text",
                description="Agent's final response text.",
                shape_type=ShapeType.TEXT,
            ),
        ],
    )


def _doc_analyze_provider() -> TaskProcessingProvider:
    return TaskProcessingProvider(
        id=PROVIDER_ID_DOC_ANALYZE,
        name="Hermiq Document Analysis (hermiq-exec)",
        task_type=TASK_TYPE_DOC_ANALYZE,
        expected_runtime=90,  # seconds; matches CLAUDE_TIMEOUT_SECONDS + retry headroom
        input_shape_enum_values={
            "flow": [
                ShapeEnumValue(name="Tender document (PvE/bestek)", value="tender-document"),
                ShapeEnumValue(name="External source (blog/docs)", value="external-source"),
                ShapeEnumValue(name="Competitor page", value="competitor"),
                ShapeEnumValue(name="Scientific paper", value="scientific-paper"),
            ],
        },
    )


def _agent_exec_provider() -> TaskProcessingProvider:
    return TaskProcessingProvider(
        id=PROVIDER_ID_AGENT_EXEC,
        name="Hermiq Agent Execution (hermiq-exec)",
        task_type=TASK_TYPE_AGENT_EXEC,
        expected_runtime=300,
    )


def register_providers(nc: NextcloudApp) -> None:
    LOGGER.info("Registering TaskProcessing providers")
    nc.providers.task_processing.register(_doc_analyze_provider(), _doc_analyze_task_type())
    nc.providers.task_processing.register(_agent_exec_provider(), _agent_exec_task_type())


def unregister_providers(nc: NextcloudApp) -> None:
    LOGGER.info("Unregistering TaskProcessing providers")
    nc.providers.task_processing.unregister(PROVIDER_ID_DOC_ANALYZE)
    nc.providers.task_processing.unregister(PROVIDER_ID_AGENT_EXEC)


# ── Workspace lifecycle (plan §6.5 mitigation: per-job workspace wipe) ──
def _workspace_root() -> Path:
    return Path(persistent_storage()) / "scratch"


def _workspace_for(task_id: int) -> Path:
    return _workspace_root() / str(task_id)


def _wipe(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# ── Task processing ──────────────────────────────────────────────────
async def _process_task(nc: NextcloudApp, task: dict[str, typing.Any]) -> None:
    global _jobs_processed

    task_id = task.get("id")
    if task_id is None:
        LOGGER.error("Task missing 'id' field — cannot process or report a result: %s", task)
        return
    task_type = task.get("type")
    task_input = task.get("input") or {}
    workspace = _workspace_for(int(task_id))

    # Pre-job wipe: guarantee no leftovers from a prior job in this same
    # persistent worker process (the isolation delta vs Specter's per-batch
    # containers — see README's honesty note).
    _wipe(workspace)

    output: dict[str, typing.Any] | None = None
    error_message: str | None = None
    try:
        if task_type in _SYNC_HANDLERS:
            output = await asyncio.to_thread(_SYNC_HANDLERS[task_type], task_input, workspace)
        elif task_type in _TUPLE_HANDLERS:
            output, error_message = await asyncio.to_thread(_TUPLE_HANDLERS[task_type], task_input, workspace)
        else:
            error_message = f"Unknown task type '{task_type}' (no handler registered in main.py)"
    except ValueError as exc:
        error_message = str(exc)
    except Exception as exc:
        LOGGER.exception("Unhandled error processing task %s (%s)", task_id, task_type)
        error_message = f"Unhandled worker error: {exc}"
    finally:
        # Post-job wipe: don't let this job's document content, temp files,
        # or any Claude CLI scratch state survive into the next poll.
        _wipe(workspace)
        _jobs_processed += 1

    result = await asyncio.to_thread(
        nc.providers.task_processing.report_result,
        task_id,
        output=output,
        error_message=error_message,
    )
    if not result:
        LOGGER.warning(
            "report_result for task %s returned no confirmation (network/AppAPI issue?)",
            task_id,
        )


async def _poll_once(nc: NextcloudApp) -> list[dict[str, typing.Any]]:
    """Fetch the next task(s) to run. See module docstring for the
    batch-poll caveat verified against this NC 33.0.0-dev checkout."""
    provider_ids = [PROVIDER_ID_DOC_ANALYZE, PROVIDER_ID_AGENT_EXEC]
    task_types = [TASK_TYPE_DOC_ANALYZE, TASK_TYPE_AGENT_EXEC]

    if USE_BATCH_POLL:
        batch = await asyncio.to_thread(
            nc.providers.task_processing.next_task_batch,
            provider_ids,
            task_types,
            POLL_BATCH_SIZE,
        )
        return [entry["task"] for entry in batch.get("tasks", []) if "task" in entry]

    single = await asyncio.to_thread(nc.providers.task_processing.next_task, provider_ids, task_types)
    task = single.get("task")
    return [task] if task else []


async def _poll_loop() -> None:
    global _poll_should_run
    nc = NextcloudApp()  # standalone instance: env-driven (APP_ID/APP_SECRET/NEXTCLOUD_URL), no inbound request
    LOGGER.info(
        "Poll worker starting (interval=%ss, batch=%s, use_batch_poll=%s, recycle_after=%s jobs)",
        POLL_INTERVAL_SECONDS,
        POLL_BATCH_SIZE,
        USE_BATCH_POLL,
        MAX_JOBS_BEFORE_RECYCLE,
    )
    while _poll_should_run:
        try:
            tasks = await _poll_once(nc)
        except Exception:
            LOGGER.exception("Poll error — backing off")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        if not tasks:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        for task in tasks:
            await _process_task(nc, task)
            if _jobs_processed >= MAX_JOBS_BEFORE_RECYCLE:
                LOGGER.warning(
                    "Recycle threshold reached (%d jobs) — exiting so Docker/AppAPI restarts the "
                    "container with a clean process (plan §6.5 mitigation: self-recycle after N jobs)",
                    _jobs_processed,
                )
                os._exit(0)  # deploy/ runs this container with `restart: unless-stopped`


def start_poll_worker() -> None:
    global _poll_task, _poll_should_run
    if _poll_should_run:
        return
    _poll_should_run = True
    _poll_task = asyncio.create_task(_poll_loop())


def stop_poll_worker() -> None:
    global _poll_task, _poll_should_run
    _poll_should_run = False
    if _poll_task is not None:
        _poll_task.cancel()
        _poll_task = None


# ── Lifespan ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_nextcloud_logging("hermiq_exec", logging_level=logging.WARNING)
    LOGGER.info("Starting hermiq-exec ExApp")
    yield
    stop_poll_worker()
    LOGGER.info("hermiq-exec ExApp shutdown complete")


# ── FastAPI App ─────────────────────────────────────────────────────
APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)


# ── Required AppAPI endpoints ─────────────────────────────────────────
@APP.get("/heartbeat")
async def heartbeat_callback():
    return JSONResponse(content={"status": "ok"})


@APP.post("/init")
async def init_callback(nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    """AppAPI calls this once after installation. hermiq-exec needs no data
    migration or model download, so init completes immediately — provider
    registration happens in /enabled, matching AppAPI's expected lifecycle
    (a disabled app should not be registered as a TaskProcessing provider)."""
    nc.set_init_status(100)
    return JSONResponse(content={})


@APP.put("/enabled")
async def enabled_callback(enabled: bool, nc: typing.Annotated[NextcloudApp, Depends(nc_app)]):
    """Runs on the event loop thread (async def, unlike n8n's sync handler)
    so start_poll_worker()'s asyncio.create_task() attaches to the right
    loop — a sync `def` FastAPI handler runs in the threadpool and has no
    running loop to attach to."""
    if enabled:
        LOGGER.info("Enabling hermiq-exec")
        try:
            await asyncio.to_thread(register_providers, nc)
        except Exception as exc:
            LOGGER.exception("Provider registration failed")
            return JSONResponse(content={"error": str(exc)})
        start_poll_worker()
    else:
        LOGGER.info("Disabling hermiq-exec")
        stop_poll_worker()
        await asyncio.to_thread(unregister_providers, nc)
    return JSONResponse(content={"error": ""})


# ── Entry Point ─────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app(APP, log_level="info")
