"""Postgres connectivity for the proxy.

One Postgres (with the pgvector extension) backs everything that must persist
across restarts: the knowledge base (chunks + embeddings), conversation history
(sessions), and crystalized memories. The DSN is the single deployment knob —
point it at the bundled compose container or a managed instance (RDS/Aurora);
nothing else changes.

Schema is owned by the versioned migration runner (``migrate``) — an explicit,
gated, once-each step, not a best-effort apply on boot. See ``migrate.py`` and
``docs/db-migrations.md``; ``migrations/`` holds the ``NNN_name.sql`` files.
"""

from __future__ import annotations

# NB: the migration *function* is imported from the ``migrate`` submodule directly
# (``from agentic_devops.db.migrate import migrate``) to avoid shadowing the module
# with a same-named package attribute. The package re-exports only the
# non-colliding helpers.
from agentic_devops.db.connection import close_all, get_pool
from agentic_devops.db.migrate import check_current, status

__all__ = ["check_current", "close_all", "get_pool", "status"]
