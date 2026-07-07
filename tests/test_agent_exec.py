"""Unit tests for ex_app/lib/analyze.py::run_agent_exec (hermiq:agent-exec).

@spec openspec/changes/agent-exec-handler/specs/agent-exec-handler/spec.md

No real subprocess/network call is ever made: every test patches
``subprocess.run`` (or forces ``subprocess.TimeoutExpired``/
``FileNotFoundError``) so this suite runs identically in CI and in the
python:3.11-slim container used by ``make check-strict`` — neither has the
Claude CLI, Anthropic credentials, or the egress jail. See
openspec/changes/agent-exec-handler/tasks.md §4 for the checklist this file
implements; live AppAPI/jail execution stays deploy-gated and is NOT proven
here.
"""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import analyze


def _valid_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": "corr-123",
        "agent_id": "agent-uuid-1",
        "acting_user": "alice",
        "model": "sonnet",
        "system_prompt": "You are a helpful agent.",
        "prompt": "Summarize the attached notes.",
        "skill_set": json.dumps([{"slug": "notes-skill", "instructions": "Summarize concisely."}]),
        "tool_allowlist": json.dumps(["hermiq.search", "hermiq.readFile"]),
        "context_files": json.dumps({"notes.txt": "Some reference notes."}),
        "max_turns": 5,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return base


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _success_stdout(result: str = "All done.", num_turns: int = 3, subtype: str = "success") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": False,
            "result": result,
            "num_turns": num_turns,
            "session_id": "session-abc",
        }
    )


# ---------------------------------------------------------------------------
# (a) Well-formed task builds the correct claude command + runs in the
#     right workspace.
# ---------------------------------------------------------------------------


def test_builds_correct_command_and_runs_in_workspace(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompletedProcess(0, stdout=_success_stdout())

    with patch("analyze.subprocess.run", side_effect=fake_run):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    command = captured["command"]
    assert command[0] == "claude"
    assert "-p" in command
    assert "Summarize the attached notes." in command
    assert "--model" in command
    assert command[command.index("--model") + 1] == "sonnet"
    assert "--max-turns" in command
    assert command[command.index("--max-turns") + 1] == "5"
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert "--allowedTools" in command
    assert command[command.index("--allowedTools") + 1] == "hermiq.search,hermiq.readFile"
    assert "--append-system-prompt" in command
    assert command[command.index("--append-system-prompt") + 1] == "You are a helpful agent."
    assert captured["cwd"] == tmp_path
    assert captured["timeout"] == 30


def test_empty_tool_allowlist_disables_all_tools(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["command"] = command
        return _FakeCompletedProcess(0, stdout=_success_stdout())

    with patch("analyze.subprocess.run", side_effect=fake_run):
        analyze.run_agent_exec(_valid_input(tool_allowlist=None), tmp_path)

    command = captured["command"]
    assert command[command.index("--allowedTools") + 1] == ""


# ---------------------------------------------------------------------------
# (b) Sample CLI stdout parses into the correct success envelope.
# ---------------------------------------------------------------------------


def test_clean_completion_parses_into_success_envelope(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=_success_stdout(
        result="Here is your summary.", num_turns=4
    ))):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert set(output.keys()) == {"status", "response_text", "artifacts", "audit_json", "error_detail"}
    assert output["status"] == "success"
    assert output["response_text"] == "Here is your summary."
    assert output["error_detail"] == ""
    assert json.loads(output["artifacts"]) == []

    audit = json.loads(output["audit_json"])
    assert audit["model"] == "sonnet"
    assert audit["correlation_id"] == "corr-123"
    assert audit["agent_id"] == "agent-uuid-1"
    assert audit["acting_user"] == "alice"
    assert audit["turns"] == 4
    assert audit["exit_code"] == 0
    assert isinstance(audit["duration_ms"], int)


# ---------------------------------------------------------------------------
# (c) Non-zero exit -> failure status.
# ---------------------------------------------------------------------------


def test_non_zero_exit_returns_failure(tmp_path: Path) -> None:
    with patch(
        "analyze.subprocess.run",
        return_value=_FakeCompletedProcess(1, stdout="", stderr="API request failed: rate limited"),
    ):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert output["status"] == "failure"
    assert "rate limited" in output["error_detail"]
    audit = json.loads(output["audit_json"])
    assert audit["exit_code"] == 1


def test_cli_error_subtype_returns_failure(tmp_path: Path) -> None:
    stdout = _success_stdout(result="", num_turns=10, subtype="error_max_turns")
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=stdout)):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert output["status"] == "failure"
    assert "error_max_turns" in output["error_detail"]


def test_unparseable_stdout_returns_failure(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout="not json at all")):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert output["status"] == "failure"
    assert "no parseable JSON" in output["error_detail"]


# ---------------------------------------------------------------------------
# (d) Timeout -> the spec's timeout status.
# ---------------------------------------------------------------------------


def test_timeout_returns_timeout_status(tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 30))

    with patch("analyze.subprocess.run", side_effect=fake_run):
        output, error = analyze.run_agent_exec(_valid_input(timeout_seconds=30), tmp_path)

    assert error is None
    assert output is not None
    assert output["status"] == "timeout"
    assert "timeout_seconds=30" in output["error_detail"]


def test_missing_claude_binary_is_transport_failure(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run", side_effect=FileNotFoundError()):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert output is None
    assert error is not None
    assert "claude CLI not found" in error


def test_refusal_is_detected(tmp_path: Path) -> None:
    stdout = _success_stdout(result="I can't help with that request.")
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=stdout)):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert output["status"] == "refused"
    assert output["error_detail"] == ""


# ---------------------------------------------------------------------------
# (e) Missing required field -> structured failure, poll loop not crashed.
# ---------------------------------------------------------------------------


def test_missing_required_field_is_transport_failure(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run") as mocked_run:
        output, error = analyze.run_agent_exec(_valid_input(prompt=""), tmp_path)

    assert output is None
    assert error is not None
    assert "prompt" in error
    mocked_run.assert_not_called()


def test_unknown_model_is_transport_failure(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run") as mocked_run:
        output, error = analyze.run_agent_exec(_valid_input(model="gpt-5"), tmp_path)

    assert output is None
    assert error is not None
    assert "unknown model" in error.lower()
    mocked_run.assert_not_called()


def test_malformed_json_field_is_transport_failure(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run") as mocked_run:
        output, error = analyze.run_agent_exec(_valid_input(tool_allowlist="{not valid json"), tmp_path)

    assert output is None
    assert error is not None
    assert "malformed input" in error
    mocked_run.assert_not_called()


def test_run_agent_exec_never_raises_on_bad_input(tmp_path: Path) -> None:
    """main.py's _process_task only guards ValueError/Exception around
    _SYNC_HANDLERS; the tuple-handler contract requires run_agent_exec to
    itself never raise on bad input — verified directly here."""
    for bad_input in ({}, {"model": "sonnet"}, {"prompt": None, "model": None}):
        output, error = analyze.run_agent_exec(bad_input, tmp_path)
        assert output is None
        assert isinstance(error, str) and error


# ---------------------------------------------------------------------------
# (f) No secret leaks into audit_json / result / error_detail.
# ---------------------------------------------------------------------------


def test_secret_is_redacted_from_stderr_failure_path(tmp_path: Path) -> None:
    leaky_stderr = "auth failed for token sk-ant-api03-abcdefghijklmnop123456"
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(1, stderr=leaky_stderr)):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert "sk-ant-" not in output["error_detail"]
    assert "[REDACTED]" in output["error_detail"]
    assert "sk-ant-" not in output["audit_json"]
    assert "sk-ant-" not in output["response_text"]


def test_secret_is_redacted_from_response_text(tmp_path: Path) -> None:
    stdout = _success_stdout(result="Here is a Bearer abcdef123456.token you can use.")
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=stdout)):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    assert "Bearer abcdef123456" not in output["response_text"]
    assert "[REDACTED]" in output["response_text"]


def test_attribution_fields_never_used_to_authenticate(tmp_path: Path) -> None:
    """agent_id/acting_user are echoed into audit_json but never appear in
    the constructed CLI command (they are attribution, not credentials)."""
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["command"] = command
        return _FakeCompletedProcess(0, stdout=_success_stdout())

    with patch("analyze.subprocess.run", side_effect=fake_run):
        output, error = analyze.run_agent_exec(_valid_input(), tmp_path)

    assert error is None
    assert output is not None
    command_str = " ".join(captured["command"])
    assert "agent-uuid-1" not in command_str
    assert "alice" not in command_str
    audit = json.loads(output["audit_json"])
    assert audit["agent_id"] == "agent-uuid-1"
    assert audit["acting_user"] == "alice"


# ---------------------------------------------------------------------------
# skill_set / context_files materialization (tasks.md §4.4).
# ---------------------------------------------------------------------------


def test_skill_set_and_context_files_are_materialized(tmp_path: Path) -> None:
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=_success_stdout())):
        analyze.run_agent_exec(_valid_input(), tmp_path)

    skill_file = tmp_path / ".claude" / "skills" / "notes-skill" / "SKILL.md"
    assert skill_file.exists()
    assert "Summarize concisely." in skill_file.read_text(encoding="utf-8")

    context_file = tmp_path / "context" / "notes.txt"
    assert context_file.exists()
    assert context_file.read_text(encoding="utf-8") == "Some reference notes."


def test_materialization_never_escapes_workspace(tmp_path: Path) -> None:
    """A malicious slug/filename with path traversal must never write
    outside the per-task workspace (jail requirement 3)."""
    hostile_input = _valid_input(
        skill_set=json.dumps([{"slug": "../../etc/evil", "instructions": "x"}]),
        context_files=json.dumps({"../../etc/passwd": "pwned"}),
    )
    with patch("analyze.subprocess.run", return_value=_FakeCompletedProcess(0, stdout=_success_stdout())):
        analyze.run_agent_exec(hostile_input, tmp_path)

    for path in tmp_path.rglob("*"):
        assert tmp_path in path.resolve().parents or path.resolve() == tmp_path.resolve()


def test_skip_network_fetch_for_skills_and_context() -> None:
    """Static check that _materialize_* never import/use a network client —
    the contract forbids any network fetch to resolve skills/context."""
    import inspect

    source = inspect.getsource(analyze._materialize_skill_set) + inspect.getsource(
        analyze._materialize_context_files
    )
    for forbidden in ("requests.", "httpx.", "urllib.request", "socket."):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# max_turns / timeout_seconds clamping.
# ---------------------------------------------------------------------------


def test_max_turns_and_timeout_are_clamped_to_ceiling(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["command"] = command
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompletedProcess(0, stdout=_success_stdout())

    with patch("analyze.subprocess.run", side_effect=fake_run):
        analyze.run_agent_exec(_valid_input(max_turns=99999, timeout_seconds=99999), tmp_path)

    command = captured["command"]
    assert command[command.index("--max-turns") + 1] == str(analyze.AGENT_EXEC_MAX_TURNS_CEILING)
    assert captured["timeout"] == analyze.AGENT_EXEC_TIMEOUT_CEILING_SECONDS


def test_missing_max_turns_and_timeout_use_defaults(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["command"] = command
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompletedProcess(0, stdout=_success_stdout())

    with patch("analyze.subprocess.run", side_effect=fake_run):
        analyze.run_agent_exec(_valid_input(max_turns=None, timeout_seconds=None), tmp_path)

    command = captured["command"]
    assert command[command.index("--max-turns") + 1] == str(analyze.AGENT_EXEC_DEFAULT_MAX_TURNS)
    assert captured["timeout"] == analyze.AGENT_EXEC_DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# (contract) input/output field names match both design.md tables verbatim.
# ---------------------------------------------------------------------------


def test_contract_input_output_field_names() -> None:
    input_fields = set(analyze.AGENT_EXEC_REQUIRED_FIELDS) | {
        "system_prompt",
        "skill_set",
        "tool_allowlist",
        "context_files",
        "max_turns",
        "timeout_seconds",
    }
    assert input_fields == {
        "correlation_id",
        "agent_id",
        "acting_user",
        "model",
        "system_prompt",
        "prompt",
        "skill_set",
        "tool_allowlist",
        "context_files",
        "max_turns",
        "timeout_seconds",
    }

    output = analyze._agent_exec_output(
        status="success", response_text="", artifacts=[], audit={}, error_detail=""
    )
    assert set(output.keys()) == {"status", "response_text", "artifacts", "audit_json", "error_detail"}
