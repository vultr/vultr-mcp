"""Tool-call audit records.

The claim this supports is "every action taken through MCP is logged", so the
tests that matter are the ones proving a record is always emitted -- including
when the tool fails -- and that it never carries payload data. A log that
quietly drops the interesting calls, or that captures a kubeconfig, is worse
than no log.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from vultr_mcp.audit import AuditMiddleware, audit_enabled


@pytest.fixture
def server():
    mcp = FastMCP("audit-test")

    @mcp.tool
    def echo(label: str, secret_token: str = "") -> dict:
        return {"label": label}

    @mcp.tool
    def explode(instance_id: str) -> dict:
        raise RuntimeError(f"upstream said: password is hunter2 for {instance_id}")

    mcp.add_middleware(AuditMiddleware())
    return mcp


async def _call(server, capsys, tool, args, expect_error=False):
    async with Client(server) as client:
        if expect_error:
            with pytest.raises(Exception):
                await client.call_tool(tool, args)
        else:
            await client.call_tool(tool, args)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if '"mcp.tool_call"' in ln]
    assert lines, "no audit record emitted"
    return json.loads(lines[-1])


def test_enabled_by_default(monkeypatch):
    """An audit trail that defaults off is not one."""
    monkeypatch.delenv("VULTR_MCP_AUDIT_LOG", raising=False)
    assert audit_enabled() is True
    monkeypatch.setenv("VULTR_MCP_AUDIT_LOG", "false")
    assert audit_enabled() is False


async def test_successful_call_is_recorded(server, capsys):
    rec = await _call(server, capsys, "echo", {"label": "web-01"})
    assert rec["event"] == "mcp.tool_call"
    assert rec["tool"] == "echo"
    assert rec["outcome"] == "ok"
    assert isinstance(rec["duration_ms"], float)
    assert rec["request_id"]
    assert rec["ts"]


async def test_argument_values_are_never_recorded(server, capsys):
    """Names identify the shape of a call; values are the caller's data."""
    rec = await _call(server, capsys, "echo", {"label": "web-01", "secret_token": "sk-live-abc123"})
    assert rec["argument_names"] == ["label", "secret_token"]
    blob = json.dumps(rec)
    assert "sk-live-abc123" not in blob
    assert "web-01" not in blob


async def test_failures_are_recorded_and_still_raise(server, capsys):
    """The failing calls are the ones an audit is most often asked about."""
    rec = await _call(server, capsys, "explode", {"instance_id": "i-1"}, expect_error=True)
    assert rec["outcome"] == "error"
    assert "duration_ms" in rec


async def test_error_chain_survives_the_toolerror_wrapper(server, capsys):
    """FastMCP wraps every failure in ToolError, so the outer type alone says
    only "a tool failed". The chain is what distinguishes one cause from
    another.
    """
    rec = await _call(server, capsys, "explode", {"instance_id": "i-1"}, expect_error=True)
    assert rec["error_type"] == "ToolError"
    assert "RuntimeError" in rec["error_chain"]


async def test_error_messages_are_not_recorded(server, capsys):
    """Upstream errors quote API responses, which can carry credentials."""
    rec = await _call(server, capsys, "explode", {"instance_id": "i-1"}, expect_error=True)
    blob = json.dumps(rec)
    assert "hunter2" not in blob
    assert "upstream said" not in blob


async def test_result_content_is_not_recorded(server, capsys):
    """Cardinality is useful after the fact; content is the thing being protected."""
    rec = await _call(server, capsys, "echo", {"label": "prod-db-01"})
    assert "prod-db-01" not in json.dumps(rec)
    assert rec.get("result_items") is None or isinstance(rec["result_items"], int)


async def test_identity_is_recorded_when_unauthenticated(server, capsys):
    """With no auth layer there is no identity to claim -- say so rather than guess."""
    rec = await _call(server, capsys, "echo", {"label": "x"})
    assert rec["auth_method"] in ("none", "unknown")


async def test_audit_failure_cannot_break_a_call(server, capsys, monkeypatch):
    """A logging bug must not turn a working tool into an error."""
    import vultr_mcp.audit as audit

    def boom(_record):
        raise RuntimeError("logging pipeline down")

    monkeypatch.setattr(audit, "emit", boom)
    async with Client(server) as client:
        result = await client.call_tool("echo", {"label": "still-works"})
    assert result is not None


# -- the file sink a log shipper tails ----------------------------------------


def _reset_writers():
    from vultr_mcp import audit

    audit._writers.clear()


def test_emit_writes_one_file_per_event_type(tmp_path, monkeypatch, capsys):
    """A shipper maps a file to a table, and the two shapes share few columns."""
    from vultr_mcp import audit

    _reset_writers()
    monkeypatch.setenv("VULTR_MCP_AUDIT_DIR", str(tmp_path))

    audit.emit({"event": "mcp.tool_call", "tool": "vultr_account_get", "outcome": "ok"})
    audit.emit({"event": "mcp.upstream_call", "path": "/v2/account", "status": 200})
    _reset_writers()

    tool_calls = (tmp_path / "mcp_tool_call.log").read_text(encoding="utf-8").strip()
    upstream = (tmp_path / "mcp_upstream_call.log").read_text(encoding="utf-8").strip()

    # The line must arrive as the JSON object it already is -- no level, no
    # second timestamp, nothing prepended by the logging machinery.
    assert json.loads(tool_calls)["tool"] == "vultr_account_get"
    assert json.loads(upstream)["status"] == 200

    # stdout keeps its copy: the file is for the shipper, stdout is for a person.
    assert '"mcp.tool_call"' in capsys.readouterr().out


def test_files_exist_empty_before_the_first_call(tmp_path, monkeypatch):
    """A tailer skips what a file already holds when it finds it, so the files
    must appear at boot, before any record could be written into them."""
    from vultr_mcp import audit

    _reset_writers()
    monkeypatch.setenv("VULTR_MCP_AUDIT_DIR", str(tmp_path))

    audit.AuditMiddleware()
    _reset_writers()

    assert (tmp_path / "mcp_tool_call.log").read_bytes() == b""
    assert (tmp_path / "mcp_upstream_call.log").read_bytes() == b""
    # Sign-ins and refreshes too: the first one after a rollout is exactly the
    # kind of record this file exists for.
    assert (tmp_path / "mcp_auth.log").read_bytes() == b""


def test_emit_still_works_when_the_directory_cannot_be_written(tmp_path, monkeypatch, capsys):
    """A log sink that takes the server down with it is worse than no sink."""
    from vultr_mcp import audit

    _reset_writers()
    # A path under a regular file cannot be created as a directory.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VULTR_MCP_AUDIT_DIR", str(blocker / "logs"))

    audit.emit({"event": "mcp.tool_call", "tool": "vultr_account_get"})
    _reset_writers()

    assert '"vultr_account_get"' in capsys.readouterr().out


def test_no_directory_configured_means_stdout_only(tmp_path, monkeypatch, capsys):
    from vultr_mcp import audit

    _reset_writers()
    monkeypatch.delenv("VULTR_MCP_AUDIT_DIR", raising=False)

    audit.emit({"event": "mcp.tool_call", "tool": "vultr_account_get"})
    _reset_writers()

    assert list(tmp_path.iterdir()) == []
    assert '"vultr_account_get"' in capsys.readouterr().out
