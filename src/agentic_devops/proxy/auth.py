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
from dataclasses import dataclass
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
