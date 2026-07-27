"""App-owned, versioned schema migrations backed by a ``schema_migrations`` ledger.

Replaces the old "apply an idempotent ``schema.sql`` best-effort on every boot"
bootstrap with an explicit, gated, once-each migration step (design + rationale:
``docs/db-migrations.md``). Key behaviours:

* **Baseline detect ("stamp, don't run").** On a DB with no ledger but the core
  tables already present, ``001`` is recorded as applied *without executing it*
  (the objects already exist); reconciliation then happens through real forward
  migrations. A fresh DB (nothing present) runs ``001..N`` normally. This is what
  unifies bootstrap and incremental into one command.
* **One transaction per migration** (Postgres transactional DDL → atomic apply,
  auto-rollback on failure), serialized by a session-level ``pg_advisory_lock`` so
  concurrent deploys can't race.
* **Checksums** detect an already-applied migration file being edited after the
  fact (an operator error); they are not a claim about historical provenance.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from importlib import resources

logger = logging.getLogger(__name__)

# Arbitrary-but-stable key for the session advisory lock that serializes migration
# runs. Any deploy touches one DB single-writer; the lock makes that safe by
# construction rather than by assumption.
_LOCK_KEY = 4_927_310_281

# Core tables whose presence (with no ledger) means "pre-migration DB → stamp the
# baseline". The two oldest tables; both predate every later schema change.
_CORE_TABLES = ("chunks", "sessions")

_DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")
_MIGRATION_NAME = re.compile(r"^(\d{3,})_([A-Za-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """Raised on an unrunnable migration state (checksum drift, bad filename, …)."""


@dataclass(frozen=True)
class Migration:
    version: str   # zero-padded 'NNN', matches the filename prefix and the ledger PK
    name: str      # human label from the filename
    sql: str       # file text (comments included; stripped at split time)
    checksum: str  # sha256 of the raw file bytes


@dataclass(frozen=True)
class PlanItem:
    version: str
    name: str
    action: str    # 'baseline-stamp' | 'apply'


@dataclass(frozen=True)
class CheckResult:
    """Read-only snapshot for the app's startup version check (no lock, no writes)."""
    ledger: bool           # does schema_migrations exist yet?
    applied: int
    pending: list[str]     # versions the code carries that the DB has not applied
    baseline_detectable: bool  # no ledger, but core tables present (a stamp would happen)

    @property
    def current(self) -> bool:
        return self.ledger and not self.pending


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #

def _migrations_dir():
    """The packaged ``migrations/`` directory as an ``importlib.resources`` Traversable."""
    return resources.files("agentic_devops.db").joinpath("migrations")


def discover() -> list[Migration]:
    """Load packaged migrations, ordered by version. Fails loudly on a duplicate
    version or a filename that doesn't match ``NNN_name.sql``."""
    out: dict[str, Migration] = {}
    for entry in _migrations_dir().iterdir():
        if not entry.name.endswith(".sql"):
            continue
        m = _MIGRATION_NAME.match(entry.name)
        if not m:
            raise MigrationError(
                f"Bad migration filename {entry.name!r}; expected 'NNN_name.sql'."
            )
        version, name = m.group(1), m.group(2)
        raw = entry.read_bytes()
        if version in out:
            raise MigrationError(f"Duplicate migration version {version!r}.")
        out[version] = Migration(
            version=version,
            name=name,
            sql=raw.decode("utf-8"),
            checksum=hashlib.sha256(raw).hexdigest(),
        )
    if not out:
        raise MigrationError("No migrations found in agentic_devops/db/migrations.")
    return [out[v] for v in sorted(out)]


def split_sql(sql: str) -> list[str]:
    """Split a migration into individual statements, respecting single-quoted
    strings, ``$tag$``-dollar-quoted bodies (so a ``DO $$ … ; … $$`` block stays
    whole), and ``--`` / ``/* */`` comments. Comments are dropped from the executed
    text; the checksum is over the raw file, so this is purely an execution concern.
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_squote = False
    dollar: str | None = None
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if dollar is not None:                       # inside $tag$ … $tag$
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                buf.append(ch)
                i += 1
            continue
        if in_squote:                                # inside '…'
            buf.append(ch)
            if ch == "'":
                if sql[i + 1 : i + 2] == "'":        # '' escape
                    buf.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if two == "--":                              # line comment → drop
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":                              # block comment → drop
            j = sql.find("*/", i)
            i = n if j == -1 else j + 2
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


# --------------------------------------------------------------------------- #
# Ledger helpers                                                               #
# --------------------------------------------------------------------------- #

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline    BOOLEAN NOT NULL DEFAULT FALSE
)
"""


def _ledger_exists(conn) -> bool:
    row = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    return row[0] is not None


def _applied(conn) -> dict[str, tuple[str, bool]]:
    """version → (checksum, baseline)."""
    rows = conn.execute(
        "SELECT version, checksum, baseline FROM schema_migrations"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _core_tables_present(conn) -> bool:
    checks = " AND ".join(f"to_regclass('public.{t}') IS NOT NULL" for t in _CORE_TABLES)
    return bool(conn.execute(f"SELECT {checks}").fetchone()[0])


def _verify_checksums(migrations: list[Migration], applied: dict[str, tuple[str, bool]]) -> None:
    for m in migrations:
        if m.version in applied and applied[m.version][0] != m.checksum:
            raise MigrationError(
                f"{m.version}_{m.name}: checksum mismatch — an already-applied "
                "migration file was modified. Migrations are immutable once shipped."
            )


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def migrate(url: str, *, dry_run: bool = False) -> list[PlanItem]:
    """Apply every pending migration to ``url`` in order. Returns the plan (what
    was, or with ``dry_run`` would be, done). Idempotent: a DB at head is a no-op.
    """
    import psycopg  # local import keeps the module import graph light

    migrations = discover()
    plan: list[PlanItem] = []

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(_LEDGER_DDL)
        if not dry_run:
            conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            applied = _applied(conn)
            _verify_checksums(migrations, applied)

            # Baseline detect: pre-migration DB → stamp 001 without executing it.
            if not applied and _core_tables_present(conn):
                base = migrations[0]
                plan.append(PlanItem(base.version, base.name, "baseline-stamp"))
                if not dry_run:
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, baseline) "
                        "VALUES (%s, %s, %s, TRUE)",
                        (base.version, base.name, base.checksum),
                    )
                    logger.info("Baseline-stamped migration %s_%s (not executed)", base.version, base.name)
                applied[base.version] = (base.checksum, True)

            # Apply the rest in order, one transaction each.
            for m in migrations:
                if m.version in applied:
                    continue
                plan.append(PlanItem(m.version, m.name, "apply"))
                if dry_run:
                    continue
                with conn.transaction():
                    for stmt in split_sql(m.sql):
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, baseline) "
                        "VALUES (%s, %s, %s, FALSE)",
                        (m.version, m.name, m.checksum),
                    )
                logger.info("Applied migration %s_%s", m.version, m.name)
                applied[m.version] = (m.checksum, False)
        finally:
            if not dry_run:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return plan


def status(url: str) -> tuple[list[tuple[str, str, object, bool]], list[Migration]]:
    """Return (applied_rows, pending_migrations) for display. Read-only, no lock.

    applied_rows: (version, name, applied_at, baseline), newest last.
    """
    import psycopg

    migrations = discover()
    with psycopg.connect(url, autocommit=True) as conn:
        if not _ledger_exists(conn):
            return [], migrations
        rows = conn.execute(
            "SELECT version, name, applied_at, baseline FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied_versions = {r[0] for r in rows}
        pending = [m for m in migrations if m.version not in applied_versions]
        return [tuple(r) for r in rows], pending


def check_current(url: str) -> CheckResult:
    """Read-only version check for app startup — never takes the lock or writes DDL."""
    import psycopg

    migrations = discover()
    with psycopg.connect(url, autocommit=True) as conn:
        if not _ledger_exists(conn):
            return CheckResult(
                ledger=False,
                applied=0,
                pending=[m.version for m in migrations],
                baseline_detectable=_core_tables_present(conn),
            )
        applied = _applied(conn)
        pending = [m.version for m in migrations if m.version not in applied]
        return CheckResult(ledger=True, applied=len(applied), pending=pending, baseline_detectable=False)
