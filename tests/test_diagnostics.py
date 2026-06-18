import json

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


def test_audit_log_written(tmp_path):
    audit = tmp_path / "audit.jsonl"
    tool = build_diagnostics_tool(audit_path=audit)
    tool.handler({"check": "disk"})
    assert audit.exists()
    record = json.loads(audit.read_text().strip().splitlines()[-1])
    assert record["check"] == "disk"
    assert record["argv"][:2] == ["df", "-h"]
