"""Native Tavily web_search tool (extended retrieval). Hermetic — httpx is stubbed."""

import httpx

from agentic_devops.tools.builtin.web import build_web_search_tool


def _tool():
    return build_web_search_tool()


def test_spec_is_read_only_and_discoverable():
    t = _tool()
    assert t.name == "web_search" and t.category == "web" and t.safety_tier == "read-only"
    assert "query" in t.input_schema["properties"] and t.input_schema["required"] == ["query"]


def test_requires_query():
    assert _tool().handler({}).startswith("ERROR: 'query'")


def test_missing_key_points_to_secrets_tab(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = _tool().handler({"query": "kubernetes crashloop"})
    assert "not configured" in out and "Secrets tab" in out


def test_formats_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "answer": "Restart the pod.",
                "results": [
                    {"title": "Fixing CrashLoopBackOff", "url": "https://ex.com/a", "content": "check logs " * 200},
                    {"title": "K8s docs", "url": "https://ex.com/b", "content": "probes"},
                ],
            }

    def fake_post(url, json=None, timeout=None):
        captured["url"], captured["json"] = url, json
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _tool().handler({"query": "crashloop", "max_results": 3, "depth": "advanced"})
    assert "Answer: Restart the pod." in out
    assert "1. Fixing CrashLoopBackOff" in out and "https://ex.com/a" in out
    assert captured["json"]["api_key"] == "tvly-test" and captured["json"]["max_results"] == 3
    assert captured["json"]["search_depth"] == "advanced"
    # snippet is capped
    assert len(out) < 5000


def test_rejected_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "bad")

    class _Resp:
        status_code = 401

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    assert "rejected" in _tool().handler({"query": "x"})


def test_max_results_clamped(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": []}

    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: (captured.update(json=json), _Resp())[1])
    _tool().handler({"query": "x", "max_results": 99})
    assert captured["json"]["max_results"] == 10  # clamped to 10
