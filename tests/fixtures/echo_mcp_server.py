"""A tiny stdio MCP server used by tests to exercise the MCP source adapter."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the provided text back, prefixed."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
