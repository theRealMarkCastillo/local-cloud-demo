"""Tiny versioned migration runner. Use Alembic for a growing application."""
from app.services import db

SQL = """
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    owner text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'complete', 'failed')),
    result_key text,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX jobs_owner_created ON jobs(owner, created_at DESC);
CREATE INDEX jobs_queue ON jobs(created_at) WHERE status = 'queued';
"""

def main():
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7823401)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_versions (version integer PRIMARY KEY)")
        if not conn.execute("SELECT 1 FROM schema_versions WHERE version=1").fetchone():
            conn.execute(SQL)
            conn.execute("INSERT INTO schema_versions VALUES (1)")
    print("Schema version 1 ready", flush=True)

if __name__ == "__main__":
    main()
