"""Integration test for the MCP source adapter: mount a real stdio MCP server
(spawned via the SDK) and call its tool through the tools-router."""

import sys
from pathlib import Path

import pytest

from agentic_devops.config import MCPServerConfig
from agentic_devops.proxy.mcp_client import MCPManager
from agentic_devops.tools.router import ToolsRouter

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


@pytest.fixture()
def manager():
    cfg = MCPServerConfig(
        name="echo", transport="stdio", command=sys.executable, args=[str(FIXTURE)]
    )
    mgr = MCPManager([cfg])
    mgr.start()
    yield mgr
    mgr.shutdown()


def test_mounts_and_calls_stdio_mcp_tool(manager):
    assert manager.errors == [], f"connect errors: {manager.errors}"

    specs = manager.tool_specs()
    names = {s.name for s in specs}
    assert "echo_echo" in names  # registered as <server>_<tool>

    router = ToolsRouter()
    for spec in specs:
        router.register(spec)

    # The MCP tool is discoverable and categorized under the server name.
    found = router.find(intent="echo back some text")
    assert any(s.name == "echo_echo" for s in found)
    assert any(s.category == "echo" for s in specs)

    # Calling it routes through the live MCP session.
    out = router.execute("echo_echo", {"text": "hello"})
    assert "echo: hello" in out


def test_empty_config_is_a_noop():
    mgr = MCPManager([])
    mgr.start()
    assert mgr.tool_specs() == []
    mgr.shutdown()


def test_write_tools_are_withheld_from_the_assistant():
    # G-2a leak-filter: a mounted tool that declares itself non-read-only
    # (readOnlyHint=False) is NOT exposed as a callable ToolSpec. Absent/None
    # hints are treated as read-only, so servers that set no annotations are
    # unaffected — only self-declared writes are withheld.
    import mcp.types as types

    from agentic_devops.proxy.mcp_client import MCPManager, _Server, _is_write_tool

    read = types.Tool(name="disk", inputSchema={"type": "object"},
                      annotations=types.ToolAnnotations(readOnlyHint=True))
    write = types.Tool(name="restart_service", inputSchema={"type": "object"},
                       annotations=types.ToolAnnotations(readOnlyHint=False))
    unhinted = types.Tool(name="query", inputSchema={"type": "object"})  # no annotations

    assert _is_write_tool(write) is True
    assert _is_write_tool(read) is False
    assert _is_write_tool(unhinted) is False  # default-safe: not withheld

    cfg = MCPServerConfig(name="host", transport="http", url="http://x/mcp")
    mgr = MCPManager([])
    mgr._servers["host"] = _Server(cfg, None, [read, write, unhinted])

    names = {s.name for s in mgr.tool_specs()}
    assert "host_disk" in names and "host_query" in names
    assert "host_restart_service" not in names          # the write is withheld
    assert mgr.excluded_write_tools == ["host:restart_service"]
