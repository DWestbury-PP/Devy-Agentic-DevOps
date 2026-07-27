"""Migration runtime: the SQL splitter (pure), discovery, and ledger status."""

from __future__ import annotations

from agentic_devops.db.migrate import check_current, discover, split_sql, status


# -- split_sql: the tricky pure function -------------------------------------

def test_split_sql_keeps_do_block_whole():
    """A ``DO $$ … ; … $$`` block is one statement despite its inner semicolons."""
    sql = """
    DO $$
    BEGIN
        PERFORM 1;
        RAISE NOTICE 'hi;there';
    END $$;
    ALTER TABLE t DROP COLUMN IF EXISTS c;
    """
    stmts = split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("DO $$")
    assert "RAISE NOTICE" in stmts[0]        # inner content kept intact
    assert stmts[1].startswith("ALTER TABLE t")


def test_split_sql_strips_comments_and_respects_string_semicolons():
    sql = """
    -- a leading comment; with a semicolon that must NOT split
    CREATE TABLE t (id text);  /* block; comment */
    INSERT INTO t VALUES ('a;b');
    """
    stmts = split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE t")
    assert "comment" not in stmts[0]         # block comment dropped
    assert stmts[1] == "INSERT INTO t VALUES ('a;b')"  # string semicolon preserved


def test_split_sql_ignores_empty_trailing():
    assert split_sql("SELECT 1;   \n  ;  ") == ["SELECT 1"]


# -- discovery ----------------------------------------------------------------

def test_discover_orders_and_has_stable_checksums():
    migs = discover()
    versions = [m.version for m in migs]
    assert versions == sorted(versions)             # ordered
    assert versions[:2] == ["001", "002"]
    assert migs[0].name == "baseline"
    assert all(len(m.checksum) == 64 for m in migs)  # sha256 hex
    # checksum is a pure function of the file → stable across calls
    assert [m.checksum for m in discover()] == [m.checksum for m in migs]


# -- ledger status against the migrated fixture DB ---------------------------

def test_fixture_db_is_at_head(pg_url):
    """The pg_url fixture migrates to head; status + check reflect that."""
    applied, pending = status(pg_url)
    applied_versions = [row[0] for row in applied]
    assert "001" in applied_versions and "002" in applied_versions
    assert pending == []

    check = check_current(pg_url)
    assert check.ledger is True
    assert check.current is True
    assert check.pending == []
