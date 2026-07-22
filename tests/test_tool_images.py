"""Image tool-results: the MCP client extracts image content into a ToolResult
instead of stringifying base64 into the model's text context."""

from types import SimpleNamespace

from agentic_devops.proxy.mcp_client import _render_result
from agentic_devops.tools.base import ToolImage, ToolResult


def _result(content, is_error=False):
    return SimpleNamespace(content=content, isError=is_error)


def test_text_only_result_stays_a_string():
    out = _render_result(_result([SimpleNamespace(text="12 datasources")]))
    assert out == "12 datasources"


def test_image_content_becomes_toolresult():
    blocks = [
        SimpleNamespace(text=None, type="image", data="BASE64PNG", mimeType="image/png"),
    ]
    out = _render_result(_result(blocks))
    assert isinstance(out, ToolResult)
    assert out.images == [ToolImage(data="BASE64PNG", mime="image/png")]
    # the base64 is NOT in the text/placeholder that would be stored or shown to the model
    assert "BASE64PNG" not in out.placeholder()
    assert "rendered" in out.placeholder().lower()


def test_mixed_text_and_image():
    blocks = [
        SimpleNamespace(text="panel: CPU Busy"),
        SimpleNamespace(text=None, type="image", data="PNGDATA", mimeType="image/png"),
    ]
    out = _render_result(_result(blocks))
    assert isinstance(out, ToolResult)
    assert out.text == "panel: CPU Busy" and len(out.images) == 1
    assert "PNGDATA" not in out.placeholder()


def test_error_result_short_circuits_to_string():
    out = _render_result(_result([SimpleNamespace(text="boom")], is_error=True))
    assert out == "ERROR: boom"


def test_image_data_uri():
    assert ToolImage(data="XYZ", mime="image/png").data_uri() == "data:image/png;base64,XYZ"
