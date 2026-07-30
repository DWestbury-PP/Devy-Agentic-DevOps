# Database migrations — `agentic-devops db migrate`

App-owned, versioned, forward-only schema evolution backed by a `schema_migrations`
ledger. This replaces the "apply an idempotent `schema.sql` best-effort on every boot"
bootstrap with an **explicit, gated, once-each** migration step the CI/CD pipeline can
reason about (remote-readiness [#2](remote-readiness.md) and [#6](remote-readiness.md)).

## Why — the current bootstrap already drifted

`schema.sql` is applied with `CREATE TABLE IF NOT EXISTS` + `ALTER … ADD COLUMN IF NOT
EXISTS`. That is **add-only and idempotent-by-existence**: it can add an object, but it
can never drop, rename, retype, or backfill one — and it can't tell you what version a
database is at. Long-lived databases therefore diverge silently from freshly-bootstrapped
ones.

This is not hypothetical. The running demo DB was probed on 2026-07-26 and carries a
legacy `token_encrypted bytea` column on both `hosts` and `github_accounts` that the
**current `schema.sql` no longer creates** (those tables moved to the `secret_ref`
Secrets-Manager-name model). `github_accounts.token_encrypted` is still *populated*
alongside a set `secret_ref`. So:

> A brand-new Devy DB and the running Devy DB are already **different shapes**.

That divergence is the expand/contract lifecycle caught mid-flight — the *expand* (add
`secret_ref`) shipped; the *contract* (drop `token_encrypted`) never did, because the
bootstrap mechanism physically cannot express a drop. The migration ledger is what forces
the contract to happen once, in order, and be recorded.

## Design

### The ledger

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,          -- 'NNN' zero-padded, matches the filename prefix
    name        TEXT NOT NULL,             -- human label from the filename
    checksum    TEXT NOT NULL,             -- sha256 of the migration file's bytes
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline    BOOLEAN NOT NULL DEFAULT FALSE  -- TRUE = stamped without executing (see below)
);
```

`checksum` guards **one thing**: it detects that an *already-applied* migration file was
edited after the fact (an operator error — migrations are immutable once shipped). It is
**not** a claim that the checksum matches whatever historically built a given database.
It can't be, and doesn't need to be.

### File convention

```
src/agentic_devops/db/migrations/
  001_baseline.sql
  002_drop_legacy_token_encrypted.sql
  003_....sql
```

- Zero-padded `NNN_snake_name.sql`, applied in lexical order.
- `001_baseline.sql` **is the current `schema.sql`** — the clean, intended shape. A fresh
  database applies it and gets exactly today's schema.
- Every later migration is plain forward DDL, run **exactly once** (the ledger guarantees
  once — later migrations need not be idempotent, though `IF EXISTS`/`IF NOT EXISTS`
  guards are welcome belt-and-suspenders).

### Baseline detection — "stamp, don't run"

An existing database already has the tables; re-running `001` there would be wrong (and,
post-drift, would still not reconcile the legacy column). So on a DB with **no
`schema_migrations` table**, the runner inspects the schema:

- **Core tables already present** (e.g. `chunks`, `sessions`, `memories` exist) → this is
  a pre-migration database. Record `001` as applied with `baseline = TRUE` **without
  executing it**, then continue at `002+`. The existing objects are trusted as-is; the
  reconciliation happens through real forward migrations (`002` drops the drift).
- **Nothing present** → a fresh database. Run `001…N` normally.

This is what unifies **bootstrap and incremental** into one command: a fresh DB and the
live DB both end up at the same version through the same code path — one by running the
baseline, the other by being stamped at it and then catching up.

### Command interface

```
agentic-devops db migrate            # apply every pending migration, in order
agentic-devops db migrate --status   # print applied vs pending; exit 0
agentic-devops db migrate --dry-run  # print the plan (what would run); apply nothing
```

- Reads the DSN from `settings.database.url` (same source as `db init`).
- **One transaction per migration.** Postgres has transactional DDL, so a failed migration
  rolls back atomically — the ledger row and the schema change commit or fail together.
- **Serialized** with `pg_advisory_lock` on a fixed key, so two concurrent deploys can't
  race the same migration. (A deploy against a live host is single-writer anyway, but the
  lock makes it safe by construction.)
- `--dry-run` and `--status` never take the lock or write.

`db init` is retained as a thin alias that runs `migrate` (so existing muscle memory and
the RDS/Aurora provisioning note keep working), but the canonical verb is `db migrate`.

### App startup no longer migrates

Today the proxy calls `apply_schema()` best-effort on every boot. That is removed. Per
remote-readiness #6, **migration is a separate, pipeline-gated step**, not something the
app does on start — otherwise a rolling app restart could silently mutate the schema out
from under a running peer. On boot the app instead does a **read-only version check**: if
the DB is behind the code's bundled migrations it logs a clear warning (and, in the
container entrypoint, the deploy is expected to have run `db migrate` as its gated
pre-step). The app never applies DDL implicitly.

For frictionless **local dev**, `./devy.sh up` gains a tiny convenience: the bundled
compose Postgres still auto-applies `001_baseline.sql` via `/docker-entrypoint-initdb.d/`
on a *first-init empty volume* (unchanged), and the wrapper runs `db migrate` once the DB
is healthy so a developer's local DB stays current without thinking about it. The
*mechanism* is identical to prod; only the *trigger* differs (wrapper convenience vs.
gated pipeline step).

## The first two migrations (grounded in the live DB)

### `001_baseline.sql`
The current `schema.sql`, verbatim. Fresh DBs run it; the live DB is stamped at it.

### `002_drop_legacy_token_encrypted.sql` — the drift fix
This is the contract step the old bootstrap could never express. It is **expand/contract
safe**: the app already reads `secret_ref` exclusively, so dropping `token_encrypted` does
not break the currently-running image — which is exactly the invariant that keeps app
rollback safe across the change.

**Guard first (option b — the teachable, safe-by-default shape):** refuse to drop if any
row still depends on the legacy column, rather than dropping blindly.

The guard is **column-existence-aware** via dynamic SQL — a subtlety the fresh-DB test
surfaced: on a fresh DB the clean baseline never created `token_encrypted`, so a *static*
reference to it in the guard would fail to parse. We check the column exists before
counting; absent → the guard and the `DROP … IF EXISTS` are both no-ops.

```sql
-- 002_drop_legacy_token_encrypted.sql (abridged — see the file for full comments)
DO $$
DECLARE orphans int := 0; n int;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name='hosts' AND column_name='token_encrypted') THEN
        EXECUTE 'SELECT count(*) FROM hosts
                 WHERE token_encrypted IS NOT NULL AND (secret_ref IS NULL OR length(secret_ref)=0)' INTO n;
        orphans := orphans + n;
    END IF;
    -- …same guard for github_accounts…
    IF orphans > 0 THEN
        RAISE EXCEPTION
          'Refusing to drop token_encrypted: % row(s) have an encrypted token but no secret_ref. '
          'Migrate those credentials into Secrets Manager and set secret_ref, then re-run.', orphans;
    END IF;
END $$;

ALTER TABLE hosts           DROP COLUMN IF EXISTS token_encrypted;
ALTER TABLE github_accounts DROP COLUMN IF EXISTS token_encrypted;
```

On the live DB this reconciles the drift (the one populated `github_accounts` row has a
set `secret_ref`, so the guard passes). On a fresh DB, `001` never created the column, so
the guard sees nothing and the `DROP … IF EXISTS` is a no-op. **After `002`, fresh and
live converge to the identical shape** — the migration system's first act is to erase the
divergence that proved it was needed.

> Because `002` deletes a column, it is deliberately **not** a pure additive/expand
> migration — it is the contract half. The rule below governs when that is safe.

## The expand/contract rule (CONTRIBUTING)

Every migration must be **backward-compatible with the currently-deployed image** — the
*previous* app version must still run correctly against the *new* schema. That is what
makes an app-tier rollback safe without a down-migration. Concretely, a schema change and
the code that depends on it are split across **two releases**:

1. **Expand** (release N): add the new column/table/index. Old code ignores it; new code
   may write it but must tolerate its absence during the rollout.
2. **Backfill + dual-read** (still release N): populate the new shape; new code reads new,
   falls back to old.
3. **Contract** (release N+1, only after N is fully rolled out and proven): drop the old
   column/constraint. Safe now because no deployed image reads it.

`token_encrypted` is the worked example: expand (`secret_ref`) landed releases ago; `002`
is the contract, safe precisely because the running image reads only `secret_ref`.

**Never** combine an expand and its own contract in a single migration, and never write a
migration that breaks the image currently in production. If you can't make a change
backward-compatible, it needs a coordinated (downtime) release, not a rolling one.

## Operational notes

- **Gated pre-step:** the deploy pipeline runs `db migrate` from the **new** image against
  the **live** Postgres *before* cycling the app tier (the DB stays up; only the app
  restarts). A migration failure aborts the deploy before any app churn.
  See `docs/deploy-design.md` §18.
- **The three datasets are untouched** by `001`/`002`: conversation history
  (`sessions`, `conversation_memories`), the pgvector knowledge base (`documents`,
  `chunks`), and crystalized memories (`memories`) carry no `token_encrypted` column and
  are not read or written by these migrations.
- **Future additive example:** embeddings are uniformly 1536-dim in the live DB, so a
  later "pin the vector dimension + add an HNSW index" change is a clean *expand* (build
  the index `CONCURRENTLY`, no data rewrite) — a good second real-world migration when KB
  scale warrants an ANN index. Out of scope here.
