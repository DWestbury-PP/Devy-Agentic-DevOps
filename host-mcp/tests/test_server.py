"""The MCP tool advertisement carries readOnlyHint annotations (G-2a) — the
signal a consuming client uses to keep mutating verbs off an assistant's surface."""

import yaml

from host_mcp.allowlist import Allowlist
from host_mcp.config import DEFAULT_ALLOWLIST
from host_mcp.server import _tool_list


def _allowlist(profile="diagnostic", allow_mutations=False):
    data = yaml.safe_load(DEFAULT_ALLOWLIST.read_text())
    return Allowlist.from_dict(
        data, active_profile=profile, allow_mutations=allow_mutations
    )


def test_read_only_checks_are_marked_read_only():
    tools = {t.name: t for t in _tool_list(_allowlist())}
    for name in ("disk", "memory", "journal", "docker_ps", "service_status"):
        assert tools[name].annotations.readOnlyHint is True, name
        assert tools[name].annotations.destructiveHint is False, name


def test_mutating_verbs_are_marked_not_read_only_when_enabled():
    tools = {t.name: t for t in _tool_list(_allowlist(allow_mutations=True))}
    for name in ("restart_service", "restart_container", "reload_config", "prune_images"):
        assert tools[name].annotations.readOnlyHint is False, name
        # reversible remediation — NOT flagged destructive (data-destroying verbs
        # are excluded from the allow-list entirely, never merely hinted).
        assert tools[name].annotations.destructiveHint is False, name


def test_no_mutating_verbs_advertised_by_default():
    # With mutations off (the default), the tool list is entirely read-only.
    tools = _tool_list(_allowlist())
    assert all(t.annotations.readOnlyHint is True for t in tools)
