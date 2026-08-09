import pytest
import yaml

import host_mcp.allowlist as al_mod
import host_mcp.config as cfg_mod
from host_mcp.allowlist import Allowlist
from host_mcp.config import DEFAULT_ALLOWLIST


def _allowlist(profile="diagnostic", allow_mutations=False):
    data = yaml.safe_load(DEFAULT_ALLOWLIST.read_text())
    return Allowlist.from_dict(
        data, active_profile=profile, allow_mutations=allow_mutations
    )


def test_profile_gating():
    diag = {c.name for c in _allowlist("diagnostic").available_checks()}
    assert {"disk", "docker_logs"} <= diag

    ro = {c.name for c in _allowlist("read-only").available_checks()}
    assert "disk" in ro
    assert "docker_ps" not in ro  # diagnostic → gated out at read-only


def test_elevated_profile_gating():
    # Profile-gating of an `elevated` check, tested against a synthetic allowlist so
    # it doesn't depend on the shipped surface carrying an elevated example (the
    # redundant systemctl_status vestige was cut in the Mac-first pass).
    data = {"checks": {"x": {"profile": "elevated", "argv": ["true"]}}}
    assert "x" not in {c.name for c in Allowlist.from_dict(data, active_profile="diagnostic").available_checks()}
    assert "x" in {c.name for c in Allowlist.from_dict(data, active_profile="elevated").available_checks()}


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
    # host + docker diagnostics that are cross-OS (present on any host)
    assert {"os_info", "network", "top_snapshot"} <= diag
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


def test_journal_query_is_linux_only(monkeypatch):
    # Linux pass: the 4-tool journal family (journal/journal_priority/journal_grep/
    # journal_unit) folded into ONE rich `journal_query` — the Linux mirror of the
    # macOS unified-log `log_query`. Linux-only; macOS uses log_query instead.
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("journal_query", {})
    assert err is None
    assert argv == ["journalctl", "--no-pager", "-n", "200"]   # no filters = recent slice
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    _, err = al.build_argv("journal_query", {})
    assert err and "not supported" in err
    # the folded-away tools no longer exist under any switch
    for gone in ("journal", "journal_priority", "journal_grep", "journal_unit"):
        assert gone not in al._checks


def test_journal_query_composes_bounds_severity_unit_grep(monkeypatch):
    # The rich primitive composes an absolute window (--since/--until), server-side
    # severity (-p), a systemd unit (-u) and text (--grep) in one indexed call —
    # each an optional flag-arg that contributes nothing when absent.
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("journal_query", {
        "lines": 50, "since": "2026-08-09 14:00:00", "until": "2026-08-09 15:00:00",
        "priority": "err", "unit": "sshd.service", "grep": "denied"})
    assert err is None
    assert argv[:4] == ["journalctl", "--no-pager", "-n", "50"]
    assert argv[argv.index("--since") + 1] == "2026-08-09 14:00:00"   # one whole token
    assert argv[argv.index("--until") + 1] == "2026-08-09 15:00:00"
    assert argv[argv.index("-p") + 1] == "err"
    assert argv[argv.index("-u") + 1] == "sshd.service"
    assert argv[argv.index("--grep") + 1] == "denied"
    # an absent flag-arg emits nothing
    argv, err = al.build_argv("journal_query", {"lines": 10})
    assert err is None and argv == ["journalctl", "--no-pager", "-n", "10"]
    # enum guards priority; unit pattern guards injection
    _, err = al.build_argv("journal_query", {"priority": "bogus"})
    assert err
    _, err = al.build_argv("journal_query", {"unit": "a; rm -rf /"})
    assert err


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
    assert argv[:4] == ["log", "show", "--last", "2d"]
    # the whole predicate is exactly one argv element — never split into flags
    assert argv[argv.index("--predicate") + 1] == 'eventMessage CONTAINS[c] "shutdown"'
    assert "--style" in argv and argv[argv.index("--style") + 1] == "compact"
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


def test_linux_journald_kernel_and_boot(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")

    # kernel-only (dmesg-style) — its own investigative door, kept separate from
    # the rich journal_query fold.
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
    # enum guards journal_boot's priority
    _, err = al.build_argv("journal_boot", {"priority": "bogus"})
    assert err


def test_linux_journal_tools_not_supported_on_macos(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    for check in ("journal_query", "journal_kernel", "journal_boot"):
        _, err = al.build_argv(check, {})
        assert err and "not supported" in err, check


def test_journal_query_grep_pattern_constraint():
    # Test the grep flag-arg's pattern directly on the ArgSpec — platform-independent
    # and the security-relevant bit (the value only ever becomes one argv token).
    spec = _allowlist("diagnostic")._checks["journal_query"].args["grep"]
    # shell metacharacters are fine as DATA — never a shell command.
    val, err = spec.validate("grep", "error|panic; rm -rf /")
    assert err is None and val == "error|panic; rm -rf /"
    # newlines and over-length patterns are rejected
    _, err = spec.validate("grep", "line1\nline2")
    assert err
    _, err = spec.validate("grep", "x" * 200)
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


def test_path_arg_confines_to_allowed_roots(tmp_path):
    # The `path` arg type is the security kernel for tail_file: a value is accepted
    # only if it realpath-resolves inside an allow-listed root — defeating traversal
    # and symlink escapes.
    from host_mcp.allowlist import ArgSpec

    allowed = tmp_path / "logs"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    spec = ArgSpec(type="path", required=True, roots=[str(allowed)])

    # a file inside the root → accepted, returned as its realpath
    ok, err = spec.validate("path", str(allowed / "app.err.log"))
    assert err is None and ok == str((allowed / "app.err.log"))

    # `..` traversal out of the root → rejected (even though the target exists)
    _, err = spec.validate("path", str(allowed / ".." / "secret.txt"))
    assert err and "allow-listed" in err

    # a sibling path entirely outside the root → rejected
    _, err = spec.validate("path", str(secret))
    assert err

    # a symlink inside the root pointing outside → rejected (realpath follows it)
    link = allowed / "escape.log"
    link.symlink_to(secret)
    _, err = spec.validate("path", str(link))
    assert err

    # control characters and empty root set → rejected
    _, err = spec.validate("path", str(allowed / "a\nb"))
    assert err
    _, err = ArgSpec(type="path", roots=[]).validate("path", "/var/log/x")
    assert err


def test_tail_file_reads_allowlisted_logs_only():
    al = _allowlist()
    assert "tail_file" in {c.name for c in al.available_checks()}  # diagnostic

    # a path under a default log root → accepted, substituted as the final token
    argv, err = al.build_argv(
        "tail_file", {"path": "/opt/homebrew/var/log/alloy.err.log", "lines": 80}
    )
    assert err is None
    assert argv[:3] == ["tail", "-n", "80"]
    assert argv[-1].endswith("/opt/homebrew/var/log/alloy.err.log")

    # arbitrary read outside the roots → rejected (no /etc/passwd, no home dir)
    _, err = al.build_argv("tail_file", {"path": "/etc/passwd"})
    assert err and "allow-listed" in err
    _, err = al.build_argv("tail_file", {"path": "/opt/homebrew/var/log/../../../../etc/passwd"})
    assert err

    # lines clamped to the declared max
    argv, err = al.build_argv("tail_file", {"path": "/var/log/system.log", "lines": 999999})
    assert err is None and "2000" in argv


def test_no_tier_c_verbs_ever_present():
    # Tier-C (data-destroying / shell) verbs are excluded permanently — not even
    # defined in the allow-list, so no switch can unlock them.
    names = set(_allowlist("elevated", allow_mutations=True)._checks)
    for forbidden in (
        "docker_exec", "docker_run", "docker_rm", "docker_stop", "docker_volume_rm",
        "docker_system_prune", "rm_rf", "shell", "bash",
    ):
        assert forbidden not in names


# -- guarded mutating actions (G-1): default-off, cross-OS, fail-closed ------
_MUTATING = {"restart_service", "restart_container", "reload_config", "prune_images"}


def test_only_curated_reversible_verbs_are_mutating():
    # The ENTIRE mutating surface is exactly the Tier-A reversible set — nothing
    # else in the allow-list is tagged `mutating`, so flipping the switch can never
    # unlock a surprise destructive verb.
    al = _allowlist("elevated", allow_mutations=True)
    tagged = {name for name, c in al._checks.items() if c.mutating}
    assert tagged == _MUTATING


def test_mutating_checks_hidden_by_default():
    # Default (allow_mutations=False): no mutating verb is exposed at ANY profile.
    for profile in ("read-only", "diagnostic", "elevated"):
        names = {c.name for c in _allowlist(profile).available_checks()}
        assert not (_MUTATING & names), f"{profile} leaked mutating checks"
    # ...and they're genuinely unavailable — can't be resolved or run by name.
    al = _allowlist("diagnostic")
    _, err = al.build_argv("restart_service", {"name": "nginx.service"})
    assert err and "unavailable" in err
    assert al.run("prune_images", {}).startswith("ERROR")


def test_mutating_checks_exposed_only_when_enabled():
    names = {c.name for c in _allowlist("diagnostic", allow_mutations=True).available_checks()}
    # reload_config is Linux/systemd-only, so it's advertised only on Linux hosts;
    # the OS-portable mutating verbs are always present when the switch is on.
    assert {"restart_service", "restart_container", "prune_images"} <= names
    if al_mod.platform.system() == "Linux":
        assert "reload_config" in names
    else:
        assert "reload_config" not in names


def test_mutations_still_require_profile_not_just_the_flag():
    # The flag governs "can act"; the read profile still governs exposure. Mutating
    # checks are profile=diagnostic, so read-only never exposes them even with the
    # flag on — read-only stays strictly look-but-don't-touch.
    names = {c.name for c in _allowlist("read-only", allow_mutations=True).available_checks()}
    assert not (_MUTATING & names)


def test_mutating_verbs_are_cross_os(monkeypatch):
    al = _allowlist("diagnostic", allow_mutations=True)
    # restart_service: systemd on Linux, brew services on macOS
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    argv, err = al.build_argv("restart_service", {"name": "nginx.service"})
    assert err is None and argv == ["systemctl", "restart", "nginx.service"]
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    argv, err = al.build_argv("restart_service", {"name": "alloy"})
    assert err is None and argv == ["brew", "services", "restart", "alloy"]
    # reload_config is Linux/systemd only — clean "not supported" on macOS
    _, err = al.build_argv("reload_config", {"name": "nginx.service"})
    assert err and "not supported" in err
    # restart_container + prune_images are docker (any OS)
    argv, err = al.build_argv("restart_container", {"container": "web"})
    assert err is None and argv == ["docker", "restart", "web"]
    argv, err = al.build_argv("prune_images", {})
    assert err is None and argv == ["docker", "image", "prune", "-a", "-f"]


def test_mutating_verbs_reject_injection():
    al = _allowlist("diagnostic", allow_mutations=True)
    _, err = al.build_argv("restart_service", {"name": "a; rm -rf /"})
    assert err
    _, err = al.build_argv("restart_container", {"container": "a; reboot"})
    assert err
    _, err = al.build_argv("restart_service", {})  # name required
    assert err


# -- config: the SecOps switch + fail-closed network auth --------------------
def _clear_host_mcp_env(monkeypatch):
    for k in ("HOST_MCP_ALLOW_MUTATIONS", "HOST_MCP_TRANSPORT", "HOST_MCP_TOKEN",
              "HOST_MCP_PROFILE", "HOST_MCP_ALLOWLIST", "HOST_MCP_AUDIT"):
        monkeypatch.delenv(k, raising=False)


def test_allow_mutations_env_flag_default_off(monkeypatch):
    _clear_host_mcp_env(monkeypatch)
    assert cfg_mod.load().allowlist.allow_mutations is False
    monkeypatch.setenv("HOST_MCP_ALLOW_MUTATIONS", "true")
    assert cfg_mod.load().allowlist.allow_mutations is True


def test_fail_closed_http_mutations_without_token(monkeypatch):
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_ALLOW_MUTATIONS", "true")
    monkeypatch.setenv("HOST_MCP_TRANSPORT", "http")
    with pytest.raises(SystemExit):  # network mutations with no token → refuse
        cfg_mod.load()


def test_http_mutations_with_token_ok(monkeypatch):
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_ALLOW_MUTATIONS", "true")
    monkeypatch.setenv("HOST_MCP_TRANSPORT", "http")
    monkeypatch.setenv("HOST_MCP_TOKEN", "secret")
    cfg = cfg_mod.load()
    assert cfg.token == "secret" and cfg.allowlist.allow_mutations is True


def test_stdio_mutations_without_token_ok(monkeypatch):
    # stdio is a local pipe, not network-reachable — exempt from the token rule.
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_ALLOW_MUTATIONS", "true")
    cfg = cfg_mod.load()
    assert cfg.transport == "stdio" and cfg.allowlist.allow_mutations is True


def test_read_only_http_without_token_unchanged(monkeypatch):
    # Back-compat: only mutations are fail-closed; read-only http with no token
    # still starts exactly as before.
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_TRANSPORT", "http")
    cfg = cfg_mod.load()  # no SystemExit
    assert cfg.allowlist.allow_mutations is False


# -- CLI switch: the restart-gated "enhanced mode" override -------------------
import host_mcp.cli as cli_mod  # noqa: E402


def test_load_allow_mutations_param_overrides_env(monkeypatch):
    # The CLI override (a literal command-line switch) enables mutations even when
    # the env var is unset; when the param is None the env is honored as before.
    _clear_host_mcp_env(monkeypatch)
    assert cfg_mod.load(allow_mutations=True).allowlist.allow_mutations is True
    assert cfg_mod.load().allowlist.allow_mutations is False       # None → env (unset)
    monkeypatch.setenv("HOST_MCP_ALLOW_MUTATIONS", "true")
    assert cfg_mod.load().allowlist.allow_mutations is True        # None → env (set)


def test_load_profile_param_overrides_env(monkeypatch):
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_PROFILE", "diagnostic")
    assert cfg_mod.load(profile="read-only").allowlist.active_profile == "read-only"


def test_load_allow_mutations_param_still_fail_closed(monkeypatch):
    # The CLI switch does NOT bypass the fail-closed rule: http + mutations + no
    # token still refuses to start (a network-reachable mutating endpoint needs auth).
    _clear_host_mcp_env(monkeypatch)
    monkeypatch.setenv("HOST_MCP_TRANSPORT", "http")
    with pytest.raises(SystemExit):
        cfg_mod.load(allow_mutations=True)


def test_cli_parses_enhanced_mode_switches():
    # Default: no flag → None (fall back to env); mutations stay off.
    args = cli_mod._parse_args([])
    assert args.allow_mutations is False and args.profile is None
    # The literal switch is present and parses; profile is choice-constrained.
    args = cli_mod._parse_args(["--allow-mutations", "--profile", "elevated"])
    assert args.allow_mutations is True and args.profile == "elevated"
    with pytest.raises(SystemExit):        # invalid profile rejected by argparse
        cli_mod._parse_args(["--profile", "root"])


# -- Mac-first surface: optional flag-args, per-OS advertising, new primitives --
def test_optional_flag_args_and_suppression():
    # Generic mechanism (OS-independent, via a synthetic check): an optional flag-arg
    # emits `--flag value` only when supplied, and can suppress a mutually-exclusive
    # placeholder — dropping the flag literal that precedes it too.
    data = {"checks": {"q": {
        "argv": ["log", "show", "--last", "{window}", "--predicate", "{predicate}"],
        "args": {
            "predicate": {"required": True, "pattern": "^.{1,50}$"},
            "window": {"default": "1h", "pattern": "^\\d+[smhd]$"},
            "start": {"flag": "--start", "suppresses": ["window"], "pattern": "^.{1,30}$"},
            "end": {"flag": "--end", "suppresses": ["window"], "pattern": "^.{1,30}$"},
        }}}}
    al = Allowlist.from_dict(data, active_profile="diagnostic")
    # default: relative window, no --start
    argv, err = al.build_argv("q", {"predicate": "process == kernel"})
    assert err is None
    assert argv == ["log", "show", "--last", "1h", "--predicate", "process == kernel"]
    # start supersedes window: --last {window} dropped, --start appended
    argv, err = al.build_argv("q", {"predicate": "p", "start": "2026-07-27 19:00:00"})
    assert err is None
    assert "--last" not in argv and "1h" not in argv
    assert argv[-2:] == ["--start", "2026-07-27 19:00:00"]
    # start + end: both appended, still no --last
    argv, err = al.build_argv("q", {"predicate": "p", "start": "a", "end": "b"})
    assert err is None
    assert "--last" not in argv and argv[-4:] == ["--start", "a", "--end", "b"]
    # an absent flag-arg emits nothing and never errors
    argv, err = al.build_argv("q", {"predicate": "p"})
    assert err is None and "--start" not in argv


def test_available_checks_filters_by_current_os(monkeypatch):
    # A check with only a Darwin (or only a Linux) argv is advertised only on that OS
    # — the basis for authoring each host to its native strengths, no LCD subset.
    # (boot_time/disk_io/reachability are now CROSS-OS, so they can't mark a side;
    # use the genuinely OS-exclusive checks as the markers.)
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    names = {c.name for c in _allowlist().available_checks()}
    assert {"log_query", "thermal_status", "power_settings", "brew_services"} <= names
    assert "journal_query" not in names and "failed_units" not in names  # Linux-only
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    names = {c.name for c in _allowlist().available_checks()}
    assert {"journal_query", "journal_kernel", "failed_units", "time_sync"} <= names
    assert "log_query" not in names and "thermal_status" not in names  # Darwin-only


def test_log_query_absolute_bounds_supersede_window(monkeypatch):
    # log_query gains start/end: supplying start drops the default --last window and
    # scopes to an absolute interval — how you target the minutes before a boot to
    # find an outage ONSET without conflating it with the recovery (boot) time.
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    al = _allowlist()
    argv, err = al.build_argv("log_query", {"predicate": 'process == "kernel"',
                                            "start": "2026-07-27 19:35:00",
                                            "end": "2026-07-27 19:41:00"})
    assert err is None
    assert "--last" not in argv
    assert argv[argv.index("--start") + 1] == "2026-07-27 19:35:00"
    assert argv[argv.index("--end") + 1] == "2026-07-27 19:41:00"
    # a bad timestamp is rejected by the pattern
    _, err = al.build_argv("log_query", {"predicate": "p", "start": "yesterday"})
    assert err
    # HH:MM without seconds is rejected up front — `log show` exits 64 on it, so the
    # pattern must not let a seconds-less time through (regression: Devy fumbled this).
    _, err = al.build_argv("log_query", {"predicate": "p", "start": "2026-07-27 19:40"})
    assert err
    # a bare date (no time) is still fine
    argv, err = al.build_argv("log_query", {"predicate": "p", "start": "2026-07-27"})
    assert err is None and argv[argv.index("--start") + 1] == "2026-07-27"


def test_new_mac_primitives_build_and_validate(monkeypatch):
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    al = _allowlist()
    assert al.build_argv("boot_time", {})[0] == ["sysctl", "-n", "kern.boottime"]
    assert al.build_argv("thermal_status", {})[0] == ["pmset", "-g", "therm"]
    assert al.build_argv("disk_io", {})[0] == ["iostat", "-c", "2"]
    assert al.build_argv("dns_config", {})[0] == ["scutil", "--dns"]
    # ping_host: defaults substituted, injection-style host rejected
    argv, err = al.build_argv("ping_host", {"host": "1.1.1.1"})
    assert err is None and argv == ["ping", "-c", "3", "-t", "5", "1.1.1.1"]
    _, err = al.build_argv("ping_host", {"host": "a; rm -rf /"})
    assert err
    # http_check: only http(s) URLs; the URL is the final, single token
    argv, err = al.build_argv("http_check", {"url": "https://api.example.com/healthz"})
    assert err is None and argv[-1] == "https://api.example.com/healthz"
    _, err = al.build_argv("http_check", {"url": "file:///etc/passwd"})
    assert err
    # genuinely macOS-only telemetry (no Linux equivalent) — cleanly "not supported"
    # on Linux, never misfire. (boot_time/disk_io/ping_host/http_check/dns_config are
    # now cross-OS and covered by test_linux_surface_primitives.)
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    for check in ("thermal_status", "power_settings", "panic_reports", "brew_services", "log_query"):
        _, err = al.build_argv(check, {})
        assert err and "not supported" in err, check


def test_linux_surface_primitives(monkeypatch):
    # The Linux strengths-first pass: identity/resource/reachability authored to
    # Linux idiom (the mirror of the Mac primitives above).
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Linux")
    al = _allowlist()
    assert al.build_argv("os_info", {})[0] == ["hostnamectl"]
    assert al.build_argv("boot_time", {})[0] == ["uptime", "-s"]
    assert al.build_argv("time_sync", {})[0] == ["timedatectl"]
    assert al.build_argv("disk_io", {})[0] == ["iostat", "-xz", "1", "2"]
    assert al.build_argv("hardware_info", {})[0] == ["lscpu"]
    assert al.build_argv("failed_units", {})[0] == ["systemctl", "--failed", "--no-pager", "--no-legend"]
    assert al.build_argv("dns_config", {})[0] == ["cat", "/etc/resolv.conf"]
    # reachability: Linux ping uses -w (deadline); getent for resolution; portable curl
    assert al.build_argv("ping_host", {"host": "1.1.1.1"})[0] == ["ping", "-c", "3", "-w", "5", "1.1.1.1"]
    assert al.build_argv("dns_lookup", {"host": "example.com"})[0] == ["getent", "hosts", "example.com"]
    argv, err = al.build_argv("http_check", {"url": "https://api.example.com/healthz"})
    assert err is None and argv[0] == "curl" and argv[-1] == "https://api.example.com/healthz"
    # injection guards still apply on the Linux argv
    _, err = al.build_argv("ping_host", {"host": "a; rm -rf /"})
    assert err
    _, err = al.build_argv("dns_lookup", {"host": "a; reboot"})
    assert err


def test_time_sync_and_failed_units_are_linux_only(monkeypatch):
    al = _allowlist()
    monkeypatch.setattr(al_mod.platform, "system", lambda: "Darwin")
    for check in ("time_sync", "failed_units"):
        _, err = al.build_argv(check, {})
        assert err and "not supported" in err, check
