"""SSO + RBAC (RBAC-1): forward-auth JWT verification, group→role mapping,
role-gated admin plane. Hermetic — an in-test RSA keypair signs tokens and the
public key is injected (no JWKS fetch)."""

import bcrypt
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agentic_devops.config import AuthConfig, DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.auth import AdminAuth, Authenticator, JwtAuth
from agentic_devops.tools.router import ToolsRouter


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub


def _sign(priv, claims):
    return pyjwt.encode(claims, priv, algorithm="RS256")


# -- JwtAuth unit tests -----------------------------------------------------
def test_maps_groups_to_roles():
    priv, pub = _keypair()
    tok = _sign(priv, {"email": "a@x.com", "groups": ["devy-admins", "other"], "iss": "idp", "aud": "devy"})
    ja = JwtAuth(public_key=pub, issuer="idp", audience="devy")
    p = ja.principal(tok, {"devy-admins": "admin", "devy-viewers": "viewer"}, {}, {}, None)
    assert p.email == "a@x.com" and p.roles == {"admin"} and p.source == "jwt"
    assert p.actor == "a@x.com"


def test_default_role_when_no_group_matches():
    priv, pub = _keypair()
    tok = _sign(priv, {"email": "b@x.com", "groups": ["unmapped"], "iss": "idp", "aud": "devy"})
    ja = JwtAuth(public_key=pub, issuer="idp", audience="devy")
    p = ja.principal(tok, {"devy-admins": "admin"}, {}, {}, "viewer")
    assert p.roles == {"viewer"}


def test_rejects_bad_signature():
    priv, _ = _keypair()
    _, other_pub = _keypair()
    tok = _sign(priv, {"email": "a@x.com", "iss": "idp", "aud": "devy"})
    with pytest.raises(Exception):
        JwtAuth(public_key=other_pub, issuer="idp", audience="devy").verify(tok)


def test_rejects_wrong_issuer_and_audience():
    priv, pub = _keypair()
    tok = _sign(priv, {"email": "a@x.com", "iss": "evil", "aud": "devy"})
    with pytest.raises(Exception):
        JwtAuth(public_key=pub, issuer="idp", audience="devy").verify(tok)
    tok2 = _sign(priv, {"email": "a@x.com", "iss": "idp", "aud": "someone-else"})
    with pytest.raises(Exception):
        JwtAuth(public_key=pub, issuer="idp", audience="devy").verify(tok2)


# -- RBAC-3: email / domain → role maps -------------------------------------
def test_resolve_roles_unions_group_email_domain():
    from agentic_devops.proxy.auth import resolve_roles

    roles = resolve_roles(
        groups=["devy-ops"], email="Boss@Example.COM",
        group_roles={"devy-ops": "operator"},
        email_roles={"boss@example.com": "admin"},   # case-insensitive match
        domain_roles={"example.com": "viewer"},
        default_role="viewer",
    )
    assert roles == {"operator", "admin", "viewer"}  # union of all three sources


def test_resolve_roles_default_when_nothing_matches():
    from agentic_devops.proxy.auth import resolve_roles

    assert resolve_roles(
        groups=[], email="nobody@nowhere.io", group_roles={}, email_roles={},
        domain_roles={}, default_role="viewer",
    ) == {"viewer"}


def test_email_map_gives_role_without_any_groups():
    # The Gmail case: no group claim at all, role comes purely from the email map.
    priv, pub = _keypair()
    tok = _sign(priv, {"email": "darrell.westbury@gmail.com", "iss": "idp", "aud": "devy"})
    ja = JwtAuth(public_key=pub, issuer="idp", audience="devy")
    p = ja.principal(tok, {}, {"darrell.westbury@gmail.com": "admin"}, {}, "viewer")
    assert p.roles == {"admin"} and p.email == "darrell.westbury@gmail.com"


def test_email_map_drives_allowed_tier(tmp_path, pool, pg_url, monkeypatch):
    # End-to-end: an email-mapped admin gets the elevated tier on the assistant plane.
    priv, pub = _keypair()
    auth = Authenticator(
        mode="jwt", admin=AdminAuth(None, None),
        jwt_auth=JwtAuth(public_key=pub, issuer="idp", audience="devy"),
        group_roles={}, email_roles={"boss@x.com": "admin"}, domain_roles={},
        default_role="viewer",
    )
    monkeypatch.setattr("agentic_devops.proxy.app.build_authenticator", lambda settings: auth)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
                          auth=AuthConfig(mode="jwt")),
        provider=object(), router=ToolsRouter(),
    )
    client = TestClient(app)
    # admin email → admin role → can reach the admin plane; unmapped → viewer → 403
    admin_tok = "Bearer " + _sign(priv, {"email": "boss@x.com", "iss": "idp", "aud": "devy"})
    assert client.get("/v1/admin/me", headers={"Authorization": admin_tok}).json()["roles"] == ["admin"]
    other_tok = "Bearer " + _sign(priv, {"email": "rando@y.com", "iss": "idp", "aud": "devy"})
    assert client.get("/v1/admin/hosts", headers={"Authorization": other_tok}).status_code == 403


def test_jwt_identity_supersedes_spoofable_x_user_id(tmp_path, pool, pg_url, monkeypatch):
    # In jwt mode the VERIFIED email scopes history — a caller can't read another
    # user's sessions by passing their X-User-Id.
    import uuid

    priv, pub = _keypair()
    auth = Authenticator(
        mode="jwt", admin=AdminAuth(None, None),
        jwt_auth=JwtAuth(public_key=pub, issuer="idp", audience="devy"),
        group_roles={}, email_roles={}, domain_roles={}, default_role="viewer",
    )
    monkeypatch.setattr("agentic_devops.proxy.app.build_authenticator", lambda settings: auth)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
                          auth=AuthConfig(mode="jwt")),
        provider=object(), router=ToolsRouter(),
    )
    client = TestClient(app)
    me = "me-" + uuid.uuid4().hex + "@x.com"
    tok = "Bearer " + _sign(priv, {"email": me, "iss": "idp", "aud": "devy"})
    # X-User-Id claims to be someone else, but the JWT email wins → scoped to `me`.
    r = client.get("/v1/sessions", headers={"Authorization": tok, "X-User-Id": "victim@x.com"})
    assert r.status_code == 200  # scoped to the verified email, not the header


# -- admin plane gated by role (jwt mode) -----------------------------------
@pytest.fixture()
def jwt_stack(tmp_path, pool, pg_url, monkeypatch):
    priv, pub = _keypair()
    auth = Authenticator(
        mode="jwt", admin=AdminAuth(None, None),
        jwt_auth=JwtAuth(public_key=pub, issuer="idp", audience="devy"),
        group_roles={"devy-admins": "admin", "devy-viewers": "viewer"}, default_role=None,
    )
    monkeypatch.setattr("agentic_devops.proxy.app.build_authenticator", lambda settings: auth)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
                          auth=AuthConfig(mode="jwt")),
        provider=object(), router=ToolsRouter(),
    )
    return TestClient(app), priv


def _bearer(priv, groups, email="u@x.com"):
    return {"Authorization": "Bearer " + _sign(priv, {"email": email, "groups": groups, "iss": "idp", "aud": "devy"})}


def test_admin_role_passes_viewer_forbidden(jwt_stack):
    client, priv = jwt_stack
    me = client.get("/v1/admin/me", headers=_bearer(priv, ["devy-admins"], "a@x.com"))
    assert me.status_code == 200 and me.json()["email"] == "a@x.com" and "admin" in me.json()["roles"]
    assert me.json()["source"] == "jwt"

    # a viewer authenticates but lacks the admin role → 403 (not 401)
    assert client.get("/v1/admin/hosts", headers=_bearer(priv, ["devy-viewers"])).status_code == 403
    # no token → 401
    assert client.get("/v1/admin/hosts").status_code == 401


def test_login_disabled_in_jwt_mode(jwt_stack):
    client, _ = jwt_stack
    assert client.post("/v1/admin/login", json={"password": "x"}).status_code == 400


def test_password_mode_still_grants_admin(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    me = c.get("/v1/admin/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["source"] == "password" and me["roles"] == ["admin"]


# -- RBAC-2: role → tier + harness tool gating ------------------------------
def test_role_tier_mapping():
    from agentic_devops.proxy.auth import max_tier_for_roles, tier_allows

    assert max_tier_for_roles({"viewer"}) == "read-only"
    assert max_tier_for_roles({"operator"}) == "diagnostic"
    assert max_tier_for_roles({"admin"}) == "elevated"
    assert max_tier_for_roles({"viewer", "operator"}) == "diagnostic"  # highest wins
    assert max_tier_for_roles(set()) == "read-only"  # unknown → most restrictive
    assert tier_allows("diagnostic", "read-only") and not tier_allows("read-only", "diagnostic")


class _FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, messages, tier, tools=None):
        return self._responses.pop(0)


def test_harness_gates_tool_by_tier():
    from agentic_devops.config import ModelTier, Settings
    from agentic_devops.proxy.harness import run_turn
    from agentic_devops.proxy.providers import ProviderResponse, ToolCall
    from agentic_devops.tools.base import ToolSpec
    from agentic_devops.tools.router import ToolsRouter

    executed: list[str] = []
    router = ToolsRouter()
    router.register(ToolSpec(
        name="run_check", category="hosts", description="run a host check",
        when_to_use="diagnostic", input_schema={"type": "object", "properties": {}},
        handler=lambda a: (executed.append("run"), "ran")[1], safety_tier="diagnostic",
    ))

    def _turn(allowed):
        executed.clear()
        prov = _FakeProvider([
            ProviderResponse(tool_calls=[ToolCall(id="c1", name="run_check", arguments={})]),
            ProviderResponse(text="done"),
        ])
        return run_turn(
            prov, router, Settings(max_iterations=4),
            messages=[{"role": "user", "content": "go"}], tier=ModelTier(model="fake"),
            tool_context={"allowed_tier": allowed},
        )

    # viewer (read-only) → the diagnostic tool is denied, never executed
    r = _turn("read-only")
    assert executed == [] and "run_check" in r.tools_used  # attempted but gated

    # operator+ (diagnostic) → executes
    _turn("diagnostic")
    assert executed == ["run"]
