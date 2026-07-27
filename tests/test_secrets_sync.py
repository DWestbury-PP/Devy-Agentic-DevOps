"""Out-of-band dev→AWS secret sync: plan classification, apply, idempotency."""

from __future__ import annotations

from agentic_devops.proxy.secrets_sync import (
    SyncItem,
    apply_sync,
    known_secret_refs,
    mask,
    plan_sync,
)

REFS = [
    ("devy/provider/anthropic", "provider key"),
    ("devy/provider/openai", "provider key"),
    ("devy/github/home", "github:home"),
]


def test_plan_classifies_add_update_unchanged_absent(make_secrets):
    source, target = make_secrets(), make_secrets()
    source.set("devy/provider/anthropic", "sk-ant-new")     # add (not in target)
    source.set("devy/provider/openai", "sk-oai-CHANGED")    # update (differs)
    target.set("devy/provider/openai", "sk-oai-OLD")
    # devy/github/home set in neither → absent

    plan = {it.ref: it for it in plan_sync(source, target, REFS)}
    assert plan["devy/provider/anthropic"].action == "add"
    assert plan["devy/provider/openai"].action == "update"
    assert plan["devy/github/home"].action == "absent"

    # An identical value classifies as unchanged.
    target.set("devy/provider/anthropic", "sk-ant-new")
    plan2 = {it.ref: it for it in plan_sync(source, target, REFS)}
    assert plan2["devy/provider/anthropic"].action == "unchanged"


def test_plan_never_exposes_values():
    """SyncItem carries only lengths — never the secret value."""
    fields = SyncItem.__dataclass_fields__
    assert "value" not in fields and "src" not in fields
    assert set(fields) == {"ref", "kind", "action", "src_len", "dst_len"}


def test_apply_writes_delta_and_is_idempotent(make_secrets):
    source, target = make_secrets(), make_secrets()
    source.set("devy/provider/anthropic", "sk-ant")
    source.set("devy/provider/openai", "sk-oai")
    target.set("devy/provider/openai", "sk-oai")            # already in sync

    items = plan_sync(source, target, REFS)
    applied, failed = apply_sync(source, target, items)
    assert not failed
    assert [it.ref for it in applied] == ["devy/provider/anthropic"]   # only the delta
    assert target.get("devy/provider/anthropic") == "sk-ant"

    # Re-run → fully in sync, nothing written (idempotent / the rotation no-op).
    applied2, failed2 = apply_sync(source, target, plan_sync(source, target, REFS))
    assert not applied2 and not failed2


def test_apply_reports_failure_on_readonly_target(make_secrets):
    source = make_secrets()
    target = make_secrets(writable=False)                   # e.g. prod-guarded / denied
    source.set("devy/provider/anthropic", "sk-ant")
    items = plan_sync(source, target, REFS)
    applied, failed = apply_sync(source, target, items)
    assert not applied
    assert failed and failed[0][0] == "devy/provider/anthropic"


def test_known_secret_refs_provider_only_without_pool(make_secrets):
    class _S:
        class secrets:
            namespace = "devy"

    refs = known_secret_refs(_S, make_secrets(), pool=None)
    kinds = {k for _, k in refs}
    assert kinds == {"provider key"}
    assert ("devy/provider/anthropic", "provider key") in refs


def test_mask_hides_value():
    assert mask(0) == "—" and mask(None) == "—"
    assert mask(42) == "•••• (len 42)"
