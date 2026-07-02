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
    p = ja.principal(tok, {"devy-admins": "admin", "devy-viewers": "viewer"}, None)
    assert p.email == "a@x.com" and p.roles == {"admin"} and p.source == "jwt"
    assert p.actor == "a@x.com"


def test_default_role_when_no_group_matches():
    priv, pub = _keypair()
    tok = _sign(priv, {"email": "b@x.com", "groups": ["unmapped"], "iss": "idp", "aud": "devy"})
    ja = JwtAuth(public_key=pub, issuer="idp", audience="devy")
    p = ja.principal(tok, {"devy-admins": "admin"}, "viewer")
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
