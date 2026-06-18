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
