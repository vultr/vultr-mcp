"""A connected client is told to refetch its tool list after a release, once, without a reconnect.

Driven over real stateless streamable HTTP, as production runs, because the whole
point is where the notification lands: on the response stream of the client's own
tools/call. The spike that chose this design found the SDK's request-less
send_tool_list_changed() is silently dropped in stateless mode.
"""

from __future__ import annotations

import base64
import json

from fastmcp import FastMCP
from starlette.testclient import TestClient

from vultr_mcp.refresh_coalescer import MemoryBackend
from vultr_mcp.tool_list_refresh import UNKNOWN_TOOL_MESSAGE, ToolListRefreshMiddleware

LIST_CHANGED = "notifications/tools/list_changed"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _proxy_token(client_id: str, jti: str) -> str:
    """A token shaped like FastMCP's proxy JWT; these servers run without auth, so it is never verified."""
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{encode({'alg': 'HS256'})}.{encode({'client_id': client_id, 'jti': jti})}.sig"


def _server(store, *, extra_tool: bool = False) -> FastMCP:
    mcp = FastMCP("refresh-test")

    @mcp.tool
    def echo(text: str) -> str:
        """Echoes the text."""
        return text

    if extra_tool:

        @mcp.tool
        def added_in_a_release() -> str:
            """Only exists in the newer release."""
            return "new"

    mcp.add_middleware(ToolListRefreshMiddleware(store=store))
    return mcp


def _call(client: TestClient, tool: str, bearer: str | None) -> list[dict]:
    """POST a tools/call and return every JSON-RPC message on its response stream, in order."""
    headers = dict(HEADERS)
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": {"text": "hi"} if tool == "echo" else {}}}
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def _methods(messages: list[dict]) -> list[str]:
    return [m.get("method") or "result" for m in messages]


def _client(server: FastMCP) -> TestClient:
    return TestClient(server.http_app(path="/mcp", stateless_http=True))


def test_first_call_carries_list_changed_before_the_result():
    with _client(_server(MemoryBackend())) as client:
        messages = _call(client, "echo", _proxy_token("claude-code-1", "a"))

    assert _methods(messages) == [LIST_CHANGED, "result"]
    assert messages[-1]["result"]["content"][0]["text"] == "hi"


def test_a_caller_already_told_is_not_told_again():
    with _client(_server(MemoryBackend())) as client:
        _call(client, "echo", _proxy_token("claude-code-1", "a"))
        again = _call(client, "echo", _proxy_token("claude-code-1", "a"))

    assert _methods(again) == ["result"]


def test_a_refreshed_token_is_still_the_same_caller():
    """The proxy re-issues the token every hour; the client registration in it is what stays."""
    with _client(_server(MemoryBackend())) as client:
        _call(client, "echo", _proxy_token("claude-code-1", "before-refresh"))
        after = _call(client, "echo", _proxy_token("claude-code-1", "after-refresh"))

    assert _methods(after) == ["result"]


def test_each_caller_is_told_separately():
    with _client(_server(MemoryBackend())) as client:
        _call(client, "echo", _proxy_token("claude-code-1", "a"))
        other = _call(client, "echo", _proxy_token("cursor-2", "b"))

    assert _methods(other) == [LIST_CHANGED, "result"]


def test_a_release_that_changes_the_tools_tells_callers_again():
    """The store outlives the process, as Redis does across a deploy; the new release's list differs."""
    store = MemoryBackend()
    token = _proxy_token("claude-code-1", "a")
    with _client(_server(store)) as client:
        _call(client, "echo", token)

    with _client(_server(store, extra_tool=True)) as client:
        after_release = _call(client, "echo", token)

    assert _methods(after_release) == [LIST_CHANGED, "result"]


def test_a_release_that_leaves_the_tools_alone_tells_nobody():
    store = MemoryBackend()
    token = _proxy_token("claude-code-1", "a")
    with _client(_server(store)) as client:
        _call(client, "echo", token)

    with _client(_server(store)) as client:
        same_tools = _call(client, "echo", token)

    assert _methods(same_tools) == ["result"]


def test_raw_api_keys_are_tracked_too():
    with _client(_server(MemoryBackend())) as client:
        first = _call(client, "echo", "VULTRAPIKEYNOTAJWT")
        second = _call(client, "echo", "VULTRAPIKEYNOTAJWT")

    assert _methods(first) == [LIST_CHANGED, "result"]
    assert _methods(second) == ["result"]


def test_a_removed_tool_explains_itself_and_triggers_a_refresh():
    token = _proxy_token("claude-code-1", "a")
    with _client(_server(MemoryBackend())) as client:
        _call(client, "echo", token)
        messages = _call(client, "list_instance_private_networks", token)

    assert _methods(messages) == [LIST_CHANGED, "result"]
    error = messages[-1]["result"]
    assert error["isError"] is True
    assert error["content"][0]["text"] == UNKNOWN_TOOL_MESSAGE.format(name="list_instance_private_networks")


def test_no_credential_means_no_notification():
    with _client(_server(MemoryBackend())) as client:
        messages = _call(client, "echo", None)

    assert _methods(messages) == ["result"]


def test_a_store_outage_never_fails_the_call():
    class Broken(MemoryBackend):
        async def get(self, key):
            raise ConnectionError("redis is down")

    with _client(_server(Broken())) as client:
        messages = _call(client, "echo", _proxy_token("claude-code-1", "a"))

    assert _methods(messages) == ["result"]
    assert messages[-1]["result"]["content"][0]["text"] == "hi"


def test_no_credential_is_kept_in_the_store():
    store = MemoryBackend()
    token = _proxy_token("claude-code-1", "secret-jti")
    with _client(_server(store)) as client:
        _call(client, "echo", token)
        _call(client, "echo", "VULTRAPIKEYNOTAJWT")

    for key in store._data:
        assert "claude-code-1" not in key and "VULTRAPIKEY" not in key and token not in key


def test_every_served_endpoint_carries_the_middleware():
    from vultr_mcp.server import create_server

    server = create_server(only_categories={"instances"})
    assert any(isinstance(m, ToolListRefreshMiddleware) for m in server.middleware)
