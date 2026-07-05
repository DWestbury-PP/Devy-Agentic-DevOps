import json

from agentic_devops.tools.builtin import diagnostics as d
from agentic_devops.tools.builtin.diagnostics import ALLOWED_CHECKS, build_diagnostics_tool


def test_disk_check_runs_and_reports_command():
    tool = build_diagnostics_tool()
    out = tool.handler({"check": "disk"})
    assert out.startswith("$ df -h")
    assert "ERROR" not in out.splitlines()[0]


def test_unknown_check_is_rejected():
    tool = build_diagnostics_tool()
    out = tool.handler({"check": "rm_rf_everything"})
    assert out.startswith("ERROR: unknown check")
    assert "rm_rf_everything" not in ALLOWED_CHECKS


def test_docker_logs_requires_valid_container():
    tool = build_diagnostics_tool()
    # Missing container
    assert tool.handler({"check": "docker_logs"}).startswith("ERROR")
    # Injection-style container name is rejected by the allow-list regex
    assert tool.handler({"check": "docker_logs", "container": "a; rm -rf /"}).startswith("ERROR")


def test_docker_logs_rejects_bad_since():
    tool = build_diagnostics_tool()
    out = tool.handler({"check": "docker_logs", "container": "web", "since": "; reboot"})
    assert out.startswith("ERROR")


def test_recent_syslog_prefers_journalctl_on_linux(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda cmd: "/usr/bin/journalctl" if cmd == "journalctl" else None)
    argv, err = d._build_argv("recent_syslog", {})
    assert err is None and argv[0] == "journalctl"


def test_recent_syslog_darwin_uses_log_show(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(d.shutil, "which", lambda cmd: "/usr/bin/log" if cmd == "log" else None)
    argv, err = d._build_argv("recent_syslog", {})
    assert err is None and argv[:2] == ["log", "show"]


def test_recent_syslog_reports_when_no_source(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(d, "Path", lambda p: type("P", (), {"exists": lambda self: False})())
    argv, err = d._build_argv("recent_syslog", {})
    assert argv is None
    assert "journalctl" in err and "host MCP" in err


def test_docker_ps_without_cli_points_to_host_mcp(monkeypatch):
    monkeypatch.setattr(d.shutil, "which", lambda cmd: None)  # no docker on PATH
    out = d.build_diagnostics_tool().handler({"check": "docker_ps"})
    assert out.startswith("ERROR") and "host_docker_ps" in out


def test_default_is_the_host_diagnostics_surface():
    tool = build_diagnostics_tool()
    assert tool.name == "host_diagnostics"
    assert tool.category == "host-diagnostics"


def test_container_scoped_yields_to_mounted_host_surface():
    # When a host MCP is mounted the builtin re-scopes to the proxy's own
    # container: distinct name + category (so find_tools no longer offers it as a
    # 'host' surface), and its text hands host questions to the host_* tools.
    tool = build_diagnostics_tool(container_scoped=True)
    assert tool.name == "proxy_self_diagnostics"
    assert tool.category == "proxy-diagnostics"
    assert tool.category != "host-diagnostics"
    blurb = (tool.description + " " + tool.when_to_use).lower()
    assert "not the target host" in blurb or "not for the host" in blurb
    assert "host_reboot_history" in tool.description or "host_journal" in tool.description
    # the checks still run — it's a real (container) diagnostics tool
    assert tool.handler({"check": "disk"}).startswith("$ df -h")


def test_recent_syslog_error_names_mounted_host_tools(monkeypatch):
    # In-container syslog failure should point at the mounted host tools by name.
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.shutil, "which", lambda cmd: None)  # no journalctl
    monkeypatch.setattr(d.Path, "exists", lambda self: False)  # no /var/log files
    out = d.build_diagnostics_tool().handler({"check": "recent_syslog"})
    assert out.startswith("ERROR")
    assert "host_journal" in out and "host_reboot_history" in out


def test_audit_log_written(tmp_path):
    audit = tmp_path / "audit.jsonl"
    tool = build_diagnostics_tool(audit_path=audit)
    tool.handler({"check": "disk"})
    assert audit.exists()
    record = json.loads(audit.read_text().strip().splitlines()[-1])
    assert record["check"] == "disk"
    assert record["argv"][:2] == ["df", "-h"]
