"""Admin control-plane authentication (Phase 9a).

Interim password auth: an admin password (bcrypt-hashed, from env) is exchanged
at ``POST /v1/admin/login`` for a short-lived signed token (HS256 JWT). Every
``/v1/admin/*`` endpoint is guarded by :meth:`AdminAuth.verify_token`.

This is deliberately the *seam* for real SSO later: replace the password check +
token issuance with a Google / Cloudflare+Okta JWT verifier, and the rest of the
admin plane (which only depends on ``verify_token`` returning a valid principal)
is unchanged. Secrets come from the environment, never ``config.yaml``:

- ``DEVY_ADMIN_PASSWORD_HASH`` — bcrypt hash of the admin password.
- ``DEVY_ADMIN_SECRET`` — HMAC signing secret for tokens.

If either is unset the admin plane is **disabled** (endpoints return 503); the
assistant plane is unaffected. Generate a hash with ``agentic-devops admin
set-password``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# 8 hours — short enough to limit a leaked token, long enough for a work session.
DEFAULT_TOKEN_TTL = 8 * 3600


@dataclass
class AdminAuth:
    password_hash: Optional[str]
    secret: Optional[str]
    token_ttl: int = DEFAULT_TOKEN_TTL

    @property
    def enabled(self) -> bool:
        return bool(self.password_hash and self.secret)

    def verify_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        import bcrypt

        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    def issue_token(self, subject: str = "admin") -> tuple[str, int]:
        """Return ``(token, expires_in_seconds)``."""
        import jwt

        now = int(time.time())
        payload = {"sub": subject, "scope": "admin", "iat": now, "exp": now + self.token_ttl}
        token = jwt.encode(payload, self.secret, algorithm="HS256")
        return token, self.token_ttl

    def verify_token(self, token: str) -> dict[str, Any]:
        """Decode + validate a token; raises on invalid/expired."""
        import jwt

        return jwt.decode(token, self.secret, algorithms=["HS256"])


def admin_auth_from_env() -> AdminAuth:
    """Build the admin auth config from the environment (.env is loaded into
    ``os.environ`` by the settings loader before this runs)."""
    return AdminAuth(
        password_hash=os.environ.get("DEVY_ADMIN_PASSWORD_HASH"),
        secret=os.environ.get("DEVY_ADMIN_SECRET"),
    )


# -- Roles → tool safety tiers (RBAC-2) -------------------------------------
# Each role grants tool use up to a maximum safety tier. Ordered least→most.
TIER_ORDER = {"read-only": 0, "diagnostic": 1, "elevated": 2}
ROLE_TIERS = {"viewer": "read-only", "operator": "diagnostic", "admin": "elevated"}


def max_tier_for_roles(roles: set[str]) -> str:
    """The highest tool safety tier the given roles permit (default read-only)."""
    best = "read-only"
    for r in roles:
        t = ROLE_TIERS.get(r)
        if t and TIER_ORDER[t] > TIER_ORDER[best]:
            best = t
    return best


def tier_allows(allowed: str, required: str) -> bool:
    """True if a caller allowed up to ``allowed`` may call a tool of ``required``."""
    return TIER_ORDER.get(required, 0) <= TIER_ORDER.get(allowed, 2)


def resolve_roles(
    *,
    groups: Any,
    email: Optional[str],
    group_roles: dict[str, str],
    email_roles: dict[str, str],
    domain_roles: dict[str, str],
    default_role: Optional[str],
) -> set[str]:
    """A principal's roles = UNION of group-, email-, and domain-mapped roles (RBAC-3).
    Falls to ``default_role`` when an authenticated user matches nothing. Email/domain
    lookups are case-insensitive. The email map is the pragmatic role source for a
    Google OIDC deployment where personal accounts carry no group claims."""
    roles = {group_roles[g] for g in (groups or []) if g in group_roles}
    if email:
        e = email.lower()
        el = {k.lower(): v for k, v in (email_roles or {}).items()}
        dl = {k.lower(): v for k, v in (domain_roles or {}).items()}
        if e in el:
            roles.add(el[e])
        domain = e.rsplit("@", 1)[1] if "@" in e else None
        if domain and domain in dl:
            roles.add(dl[domain])
    if not roles and default_role:
        roles = {default_role}
    return roles


# -- Identity + roles (RBAC-1) ----------------------------------------------
@dataclass
class Principal:
    """The authenticated caller. ``id`` is a stable identifier (email in jwt mode,
    ``sub`` in password mode); ``roles`` gate the control plane."""

    id: str
    email: Optional[str]
    roles: set[str]
    source: str  # "password" | "jwt"
    claims: dict[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def actor(self) -> str:
        """Audit label — the email if we have it, else the id."""
        return self.email or self.id


@dataclass
class JwtAuth:
    """Verify a forward-auth JWT from an edge proxy against the IdP's JWKS.

    Devy is *not* an OAuth client — an upstream (Cloudflare Access / Okta+ALB /
    oauth2-proxy) authenticates and forwards the signed JWT; we verify signature +
    issuer + audience and read identity from the claims. ``public_key`` is a test
    seam (inject a key to avoid a JWKS fetch)."""

    jwks_url: Optional[str] = None
    public_key: Any = None
    algorithms: tuple[str, ...] = ("RS256",)
    issuer: Optional[str] = None
    audience: Optional[str] = None
    email_claim: str = "email"
    groups_claim: str = "groups"
    _jwk_client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.jwks_url or self.public_key)

    def _key(self, token: str) -> Any:
        if self.public_key is not None:
            return self.public_key
        if self.jwks_url:
            if self._jwk_client is None:
                from jwt import PyJWKClient

                self._jwk_client = PyJWKClient(self.jwks_url)
            return self._jwk_client.get_signing_key_from_jwt(token).key
        raise RuntimeError("JwtAuth has no jwks_url or public_key configured")

    def verify(self, token: str) -> dict[str, Any]:
        import jwt

        return jwt.decode(
            token, self._key(token), algorithms=list(self.algorithms),
            issuer=self.issuer, audience=self.audience,
            options={"verify_aud": self.audience is not None},
        )

    def principal(
        self,
        token: str,
        group_roles: dict[str, str],
        email_roles: dict[str, str],
        domain_roles: dict[str, str],
        default_role: Optional[str],
    ) -> Principal:
        claims = self.verify(token)
        email = claims.get(self.email_claim)
        groups = claims.get(self.groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        roles = resolve_roles(
            groups=groups, email=email, group_roles=group_roles,
            email_roles=email_roles, domain_roles=domain_roles, default_role=default_role,
        )
        return Principal(
            id=email or str(claims.get("sub", "unknown")), email=email,
            roles=roles, source="jwt", claims=claims,
        )


@dataclass
class Authenticator:
    """Resolves a request's bearer/JWT into a :class:`Principal`, per ``auth.mode``.

    THE seam the whole control plane depends on. password mode preserves the
    existing admin flow (token → ``admin`` role); jwt mode verifies a forward-auth
    JWT and maps IdP groups → roles."""

    mode: str
    admin: AdminAuth
    jwt_auth: Optional[JwtAuth]
    group_roles: dict[str, str]
    default_role: Optional[str]
    email_roles: dict[str, str] = field(default_factory=dict)
    domain_roles: dict[str, str] = field(default_factory=dict)
    header: str = "Authorization"

    @property
    def enabled(self) -> bool:
        return self.admin.enabled if self.mode == "password" else bool(self.jwt_auth and self.jwt_auth.configured)

    @property
    def login_enabled(self) -> bool:
        return self.mode == "password" and self.admin.enabled

    def extract_token(self, header_value: Optional[str]) -> Optional[str]:
        if not header_value:
            return None
        if header_value.lower().startswith("bearer "):
            return header_value.split(" ", 1)[1].strip()
        return header_value.strip()

    def principal(self, token: str) -> Principal:
        """Verify a token → Principal. Raises on invalid/expired."""
        if self.mode == "password":
            claims = self.admin.verify_token(token)
            return Principal(
                id=str(claims.get("sub", "admin")), email=None,
                roles={"admin"}, source="password", claims=claims,
            )
        assert self.jwt_auth is not None
        return self.jwt_auth.principal(
            token, self.group_roles, self.email_roles, self.domain_roles, self.default_role,
        )


def build_authenticator(settings: Any) -> Authenticator:
    """Assemble the authenticator from env (password secrets) + config (jwt/rbac)."""
    admin = admin_auth_from_env()
    ac = settings.auth
    jwt_auth = None
    if ac.mode == "jwt":
        jwt_auth = JwtAuth(
            jwks_url=ac.jwks_url, algorithms=tuple(ac.algorithms), issuer=ac.issuer,
            audience=ac.audience, email_claim=ac.email_claim, groups_claim=ac.groups_claim,
        )
    return Authenticator(
        mode=ac.mode, admin=admin, jwt_auth=jwt_auth,
        group_roles=dict(settings.rbac.group_roles), default_role=settings.rbac.default_role,
        email_roles=dict(settings.rbac.email_roles), domain_roles=dict(settings.rbac.domain_roles),
        header=ac.header,
    )
