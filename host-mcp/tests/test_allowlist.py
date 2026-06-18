import yaml

from host_mcp.allowlist import Allowlist
from host_mcp.config import DEFAULT_ALLOWLIST


def _allowlist(profile="diagnostic"):
    data = yaml.safe_load(DEFAULT_ALLOWLIST.read_text())
    return Allowlist.from_dict(data, active_profile=profile)


def test_profile_gating():
    diag = {c.name for c in _allowlist("diagnostic").available_checks()}
    assert {"disk", "docker_logs"} <= diag
    assert "systemctl_status" not in diag  # elevated → gated out at diagnostic

    assert "systemctl_status" in {c.name for c in _allowlist("elevated").available_checks()}

    ro = {c.name for c in _allowlist("read-only").available_checks()}
    assert "disk" in ro
    assert "docker_ps" not in ro  # diagnostic → gated out at read-only


def test_disk_runs():
    assert _allowlist().run("disk", {}).startswith("$ df -h")


def test_unknown_and_gated_checks_error():
    assert _allowlist().run("rm_rf", {}).startswith("ERROR")
    # a real check that's gated out at the active profile is also unavailable
    assert _allowlist("read-only").run("docker_ps", {}).startswith("ERROR")


def test_docker_logs_validation():
    al = _allowlist()
    # missing required container
    _, err = al.build_argv("docker_logs", {})
    assert err
    # injection-style container rejected by pattern
    _, err = al.build_argv("docker_logs", {"container": "a; rm -rf /"})
    assert err
    # valid, with tail clamped to max and placeholders substituted
    argv, err = al.build_argv("docker_logs", {"container": "web", "tail": 99999})
    assert err is None
    assert argv[:3] == ["docker", "logs", "--tail"]
    assert "1000" in argv      # tail clamped from 99999 to max 1000
    assert argv[-1] == "web"   # container substituted as the final, separate token


def test_docker_logs_bad_since():
    _, err = _allowlist().build_argv("docker_logs", {"container": "web", "since": "; reboot"})
    assert err


def test_json_schema_marks_required_and_types():
    check = next(c for c in _allowlist().available_checks() if c.name == "docker_logs")
    schema = check.json_schema()
    assert "container" in schema["required"]
    assert schema["properties"]["tail"]["type"] == "integer"
    assert schema["properties"]["tail"]["maximum"] == 1000


def test_expanded_checks_exposed_at_diagnostic():
    diag = {c.name for c in _allowlist("diagnostic").available_checks()}
    # host + docker diagnostics added in this phase
    assert {"os_info", "network", "top_snapshot", "journal", "journal_grep"} <= diag
    assert {"docker_ps_all", "docker_inspect", "docker_stats", "docker_top",
            "docker_images", "docker_system_df"} <= diag


def test_docker_inspect_and_top_require_valid_container():
    al = _allowlist()
    for check in ("docker_inspect", "docker_top"):
        _, err = al.build_argv(check, {})
        assert err, f"{check} should require a container"
        _, err = al.build_argv(check, {"container": "a; rm -rf /"})
        assert err, f"{check} should reject injection-style names"
        argv, err = al.build_argv(check, {"container": "web"})
        assert err is None
        assert argv[-1] == "web"  # substituted as a single, final token


def test_journal_grep_pattern_constraint():
    # journal_* are Linux-only (platform-gated), so test the pattern constraint
    # directly on the ArgSpec — platform-independent and the security-relevant
    # bit. Single-argv-token substitution is covered by the docker_* tests.
    spec = _allowlist("diagnostic")._checks["journal_grep"].args["pattern"]
    # shell metacharacters are fine as DATA — they only ever become one argv
    # token, never a shell command.
    val, err = spec.validate("pattern", "error|panic; rm -rf /")
    assert err is None and val == "error|panic; rm -rf /"
    # newlines and over-length patterns are rejected
    _, err = spec.validate("pattern", "line1\nline2")
    assert err
    _, err = spec.validate("pattern", "x" * 200)
    assert err


def test_no_mutating_docker_checks_present():
    # The allowlist must never expose shell or state-changing docker verbs.
    names = set(_allowlist("elevated")._checks)  # every defined check, any profile
    for forbidden in ("docker_exec", "docker_run", "docker_rm", "docker_stop", "shell", "bash"):
        assert forbidden not in names
