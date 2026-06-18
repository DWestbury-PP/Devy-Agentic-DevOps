from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.router import FIND_TOOLS_NAME, ToolNotFoundError, ToolsRouter

import pytest


def _spec(name="echo", category="util", **kw):
    return ToolSpec(
        name=name,
        category=category,
        description=kw.get("description", "Echoes its input back."),
        when_to_use=kw.get("when_to_use", "When you need to repeat text."),
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=kw.get("handler", lambda args: args.get("text", "")),
        use_cases=kw.get("use_cases", ["repeat text"]),
    )


def test_register_and_execute():
    router = ToolsRouter()
    router.register(_spec(handler=lambda args: f"got:{args['text']}"))
    assert "echo" in router
    assert len(router) == 1
    assert router.execute("echo", {"text": "hi"}) == "got:hi"


def test_duplicate_registration_rejected():
    router = ToolsRouter()
    router.register(_spec())
    with pytest.raises(ValueError):
        router.register(_spec())


def test_reserved_name_rejected():
    router = ToolsRouter()
    with pytest.raises(ValueError):
        router.register(_spec(name=FIND_TOOLS_NAME))


def test_execute_unknown_raises():
    router = ToolsRouter()
    with pytest.raises(ToolNotFoundError):
        router.execute("nope", {})


def test_find_by_intent_scores_relevance():
    router = ToolsRouter()
    router.register(
        _spec(
            name="host_diagnostics",
            category="host-diagnostics",
            when_to_use="check disk and memory health of the host",
            use_cases=["disk space", "memory pressure"],
        )
    )
    router.register(_spec(name="weather", category="fun", when_to_use="get the weather"))

    results = router.find(intent="check disk health on the host")
    assert results[0].name == "host_diagnostics"


def test_find_by_category_filters():
    router = ToolsRouter()
    router.register(_spec(name="a", category="host-diagnostics"))
    router.register(_spec(name="b", category="fun"))
    results = router.find(category="fun")
    assert [r.name for r in results] == ["b"]


def test_find_tools_schema_lists_categories():
    router = ToolsRouter()
    router.register(_spec(category="host-diagnostics"))
    schema = router.find_tools_schema()
    assert schema["function"]["name"] == FIND_TOOLS_NAME
    assert "host-diagnostics" in schema["function"]["parameters"]["properties"]["category"]["description"]
