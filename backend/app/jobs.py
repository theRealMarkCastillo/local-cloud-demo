"""Owner-scoped job access shared by the browser API and the MCP service."""
import logging
import uuid
from app import settings as s
from app.services import db, redis, storage

log = logging.getLogger(__name__)


def create_job(owner: str, title: str, text: str) -> str:
    job_id = uuid.uuid4()
    with db() as conn:
        conn.execute("INSERT INTO jobs(id, owner, title, body) VALUES (%s,%s,%s,%s)",
                     (job_id, owner, title, text))
    # Postgres is the durable queue. Redis wake-ups are expendable; polling recovers misses.
    try:
        redis.lpush("jobs:wakeup", "1")
        redis.ltrim("jobs:wakeup", 0, 999)
    except Exception:
        log.warning("Redis wake-up unavailable; worker will poll Postgres")
    return str(job_id)


def list_jobs(owner: str) -> list[dict]:
    with db() as conn:
        return conn.execute("SELECT id,title,status,attempts,created_at,finished_at FROM jobs WHERE owner=%s ORDER BY created_at DESC LIMIT 50", (owner,)).fetchall()


def read_report(owner: str, job_id: uuid.UUID) -> bytes | None:
    with db() as conn:
        row = conn.execute("SELECT result_key FROM jobs WHERE id=%s AND owner=%s AND status='complete'", (job_id, owner)).fetchone()
    if not row:
        return None
    obj = storage().get_object(s.S3_BUCKET, row["result_key"])
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()
