"""PgSessionStore: round-trip, user_id scoping, and recall listing."""

import pytest

from agentic_devops.proxy.sessions import PgSessionStore


@pytest.fixture()
def store(pool):
    return PgSessionStore(pool)


def test_new_session_has_id_and_optional_user(store):
    s = store.new(user_id="alice")
    assert s.id and len(s.id) == 12
    assert s.user_id == "alice"


def test_save_then_load_round_trips(store):
    s = store.new(user_id="bob")
    s.add_user("how is the cluster?")
    s.add_assistant("All green.")
    store.save(s)

    loaded = store.load(s.id)
    assert loaded.user_id == "bob"
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[0]["content"] == "how is the cluster?"


def test_load_unknown_id_returns_fresh(store):
    s = store.load("does-not-exist", user_id="carol")
    assert s.id == "does-not-exist"
    assert s.messages == []
    assert s.user_id == "carol"


def test_list_for_user_scopes_and_previews(store):
    a = store.new(user_id="dave")
    a.add_user("first question about disks")
    a.add_assistant("answer")
    store.save(a)

    b = store.new(user_id="erin")
    b.add_user("someone else's chat")
    store.save(b)

    daves = store.list_for_user("dave")
    assert [row.id for row in daves] == [a.id]
    assert daves[0].preview == "first question about disks"
    assert daves[0].turns == 2
    assert store.list_for_user("nobody") == []


def test_save_preserves_user_id_when_later_omitted(store):
    s = store.new(user_id="frank")
    s.add_user("hello")
    store.save(s)

    # Reload without re-supplying the user_id, add a turn, save again.
    again = store.load(s.id)
    again.add_assistant("hi")
    again.user_id = None  # simulate a request that didn't carry identity
    store.save(again)

    assert store.load(s.id).user_id == "frank"  # COALESCE kept it
