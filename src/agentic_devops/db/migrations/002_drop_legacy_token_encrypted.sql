-- 002 — drop the legacy inline-token column now that the app reads secret_ref only.
--
-- Contract step (see docs/db-migrations.md, "expand/contract"). `hosts` and
-- `github_accounts` moved from an inline-encrypted `token_encrypted bytea` to the
-- `secret_ref` Secrets-Manager-name model releases ago; the running image reads
-- ONLY secret_ref, so removing token_encrypted does not break it — which is what
-- makes this safe to roll an app back across.
--
-- This is the first migration to remove a column, and the divergence it fixes is
-- real: a long-lived DB bootstrapped before the secret_ref era still carries
-- token_encrypted, while a fresh DB (baseline 001) never had it. After this runs,
-- both shapes converge.
--
-- Guard first (option b): refuse to drop if any row still holds a credential ONLY
-- in the legacy column (token_encrypted set, secret_ref absent) — dropping would
-- lose the sole copy. An operator must move that credential into Secrets Manager
-- and set secret_ref, then re-run. Postgres transactional DDL means the RAISE
-- aborts the whole migration atomically; nothing is dropped.
--
-- The guard is column-existence-aware via dynamic SQL: on a FRESH DB the clean
-- baseline (001) never created token_encrypted, so a static reference to it would
-- fail to parse. We check the column exists before counting; when it's absent the
-- guard is a no-op and the DROP IF EXISTS below is likewise a no-op.

DO $$
DECLARE
    orphans int := 0;
    n int;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'hosts'
                 AND column_name = 'token_encrypted') THEN
        EXECUTE 'SELECT count(*) FROM hosts
                 WHERE token_encrypted IS NOT NULL
                   AND (secret_ref IS NULL OR length(secret_ref) = 0)' INTO n;
        orphans := orphans + n;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'github_accounts'
                 AND column_name = 'token_encrypted') THEN
        EXECUTE 'SELECT count(*) FROM github_accounts
                 WHERE token_encrypted IS NOT NULL
                   AND (secret_ref IS NULL OR length(secret_ref) = 0)' INTO n;
        orphans := orphans + n;
    END IF;
    IF orphans > 0 THEN
        RAISE EXCEPTION
          'Refusing to drop token_encrypted: % row(s) have an encrypted token but no secret_ref. '
          'Migrate those credentials into Secrets Manager and set secret_ref, then re-run.', orphans;
    END IF;
END $$;

ALTER TABLE hosts           DROP COLUMN IF EXISTS token_encrypted;
ALTER TABLE github_accounts DROP COLUMN IF EXISTS token_encrypted;
