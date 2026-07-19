import yaml

import host_mcp.allowlist as al_mod
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


def test_journal_is_cross_os(monkeypatch):
    # The server auto-detects the OS (platform.system()) and picks the right argv:
    # journald on Linux, the unified log (`log show`) on macOS — same check, one server.
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("journal", {})
    assert err is None and argv[0] == "journalctl"
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("journal", {})
    assert err is None and argv[:2] == ["log", "show"]


def test_journal_grep_cross_os_substitutes_pattern(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("journal_grep", {"pattern": "panic", "lines": 50})
    assert err is None
    assert argv[0] == "grep" and "/var/log/system.log" in argv
    assert "50" in argv                              # -m {lines} (max matches)
    assert argv[argv.index("-e") + 1] == "panic"     # pattern via -e, its own token


def test_journal_unit_stays_linux_only(monkeypatch):
    # Genuinely systemd-specific checks report cleanly on macOS rather than misfiring.
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    _, err = al.build_argv("journal_unit", {"unit": "nginx.service"})
    assert err and "not supported" in err


def test_reboot_history_is_read_only_and_portable(monkeypatch):
    # Reboot history is a read-only check exposed even at the lowest profile, and
    # its `last -n N reboot` argv is identical on Linux and macOS (both accept -n).
    ro = {c.name for c in _allowlist("read-only").available_checks()}
    assert "reboot_history" in ro
    al = _allowlist("read-only")
    for osname in ("Linux", "Darwin"):
        monkeypatch.setattr(al_mod.platform, "system", lambda os=osname: os)
        argv, err = al.build_argv("reboot_history", {"lines": 5})
        assert err is None
        assert argv == ["last", "-n", "5", "reboot"]
    # default + clamp
    argv, err = al.build_argv("reboot_history", {})
    assert err is None and argv == ["last", "-n", "10", "reboot"]
    argv, err = al.build_argv("reboot_history", {"lines": 99999})
    assert err is None and "100" in argv  # clamped to max


def test_log_query_is_macos_only_and_passes_predicate_as_one_token(monkeypatch):
    al = _allowlist()
    # macOS: predicate + window substituted as whole tokens (no shell, no split).
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv(
        "log_query",
        {"predicate": 'eventMessage CONTAINS[c] "shutdown"', "window": "2d"},
    )
    assert err is None
    assert argv[:3] == ["log", "show", "--last"]
    assert "2d" in argv
    # the whole predicate is exactly one argv element — never split into flags
    assert 'eventMessage CONTAINS[c] "shutdown"' in argv
    assert argv[argv.index("--predicate") + 1] == 'eventMessage CONTAINS[c] "shutdown"'
    assert argv[-2:] == ["--style", "compact"]
    # Linux: not a systemd concept — report cleanly, don't misfire.
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    _, err = al.build_argv("log_query", {"predicate": 'process == "kernel"'})
    assert err and "not supported" in err


def test_log_query_predicate_and_window_constraints():
    checks = _allowlist("diagnostic")._checks["log_query"].args
    # newline / over-length predicates rejected
    _, err = checks["predicate"].validate("predicate", "a\nb")
    assert err
    _, err = checks["predicate"].validate("predicate", "x" * 300)
    assert err
    # quotes/brackets are fine as DATA (one argv token, never a shell)
    val, err = checks["predicate"].validate("predicate", 'eventMessage CONTAINS[c] "panic"')
    assert err is None and val == 'eventMessage CONTAINS[c] "panic"'
    # window must look like 30m / 2h / 3d
    _, err = checks["window"].validate("window", "yesterday")
    assert err
    val, err = checks["window"].validate("window", "3d")
    assert err is None and val == "3d"


def test_log_query_declares_a_longer_timeout():
    # `log show` needs more than the 20s default for wide windows.
    assert _allowlist("diagnostic")._checks["log_query"].timeout == 90


def test_run_honours_per_check_timeout(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise al_mod.subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(al_mod.subprocess, "run", fake_run)
    out = _allowlist().run("log_query", {"predicate": 'eventMessage CONTAINS[c] "x"'})
    assert captured["timeout"] == 90        # per-check override, not the 20s default
    assert "timed out after 90s" in out


def test_panic_reports_macos_only(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("panic_reports", {})
    assert err is None
    assert argv == ["ls", "-lt", "/Library/Logs/DiagnosticReports"]
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    _, err = al.build_argv("panic_reports", {})
    assert err and "not supported" in err


def test_linux_journald_native_filters(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")

    # severity filter (indexed, server-side)
    argv, err = al.build_argv("journal_priority", {})
    assert err is None
    assert argv == ["journalctl", "--no-pager", "-p", "err", "-n", "200"]
    # enum guards the priority value
    _, err = al.build_argv("journal_priority", {"priority": "bogus"})
    assert err

    # kernel-only (dmesg-style)
    argv, err = al.build_argv("journal_kernel", {"lines": 50})
    assert err is None
    assert argv == ["journalctl", "--no-pager", "-k", "-n", "50"]

    # previous-boot slice for reboot/crash RCA
    argv, err = al.build_argv("journal_boot", {})
    assert err is None
    assert argv == ["journalctl", "--no-pager", "-b", "-1", "-p", "warning", "-n", "300"]
    # boot offset is pattern-guarded (no injection, no arbitrary flags)
    _, err = al.build_argv("journal_boot", {"boot": "; reboot"})
    assert err


def test_linux_journald_filters_not_supported_on_macos(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    for check in ("journal_priority", "journal_kernel", "journal_boot"):
        _, err = al.build_argv(check, {})
        assert err and "not supported" in err, check


def test_journal_grep_pattern_constraint():
    # Test the pattern constraint directly on the ArgSpec — platform-independent and
    # the security-relevant bit. Single-argv-token substitution is covered elsewhere.
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


def test_connections_is_cross_os_with_process_attribution(monkeypatch):
    # `connections` maps sockets to the owning process — the gap `network`
    # (listening sockets only) leaves. ss -p on Linux, lsof on macOS.
    al = _allowlist()
    assert "connections" in {c.name for c in al.available_checks()}  # diagnostic
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("connections", {})
    assert err is None and argv == ["ss", "-tunap"]   # -p = process attribution
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("connections", {})
    assert err is None and argv[0] == "lsof" and "-iTCP" in argv
    # gated below diagnostic (reveals remote endpoints + process names)
    assert "connections" not in {c.name for c in _allowlist("read-only").available_checks()}


def test_services_is_cross_os(monkeypatch):
    # Same concept ("what services exist and are they up?"), resolved per-OS:
    # systemd on Linux (RHEL + Ubuntu), launchd on macOS — one server, one check.
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("services", {})
    assert err is None and argv[:2] == ["systemctl", "list-units"]
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("services", {})
    assert err is None and argv == ["launchctl", "list"]


def test_service_status_cross_os_substitutes_name(monkeypatch):
    al = _allowlist()
    # Linux: systemd unit as the final, separate token.
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("service_status", {"name": "nginx.service"})
    assert err is None and argv == ["systemctl", "status", "--no-pager", "nginx.service"]
    # macOS: launchd label (dotted) as the final token.
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("service_status", {"name": "homebrew.mxcl.alloy"})
    assert err is None and argv == ["launchctl", "list", "homebrew.mxcl.alloy"]
    # required + injection-guarded
    _, err = al.build_argv("service_status", {})
    assert err
    _, err = al.build_argv("service_status", {"name": "a; rm -rf /"})
    assert err


def test_brew_services_is_macos_only(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("brew_services", {})
    assert err is None and argv == ["brew", "services", "list"]
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    _, err = al.build_argv("brew_services", {})
    assert err and "not supported" in err


def test_no_mutating_docker_checks_present():
    # The allowlist must never expose shell or state-changing docker verbs.
    names = set(_allowlist("elevated")._checks)  # every defined check, any profile
    for forbidden in ("docker_exec", "docker_run", "docker_rm", "docker_stop", "shell", "bash"):
        assert forbidden not in names
