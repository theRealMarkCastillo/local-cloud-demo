"""Postgres-backed jobs, Redis wake-ups, private S3 reports. At-least-once execution."""
import io
import json
import logging
import re
import signal
import time
from collections import Counter
from app import settings as s
from app.services import db, redis, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
stop = False

def shutdown(*_):
    global stop
    stop = True

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

def process_one():
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not row:
            return False
        attempt = row["attempts"] + 1
        try:
            # Short demo job holds its row lock until completion. Crash => rollback/retry.
            # For long jobs, replace this with leases + heartbeat + bounded retries.
            words = re.findall(r"[\w'-]+", row["body"].lower())
            result = {"job_id": str(row["id"]), "title": row["title"], "words": len(words),
                      "characters": len(row["body"]), "top_words": Counter(words).most_common(10)}
            key = f"reports/{row['owner']}/{row['id']}.json"
            data = json.dumps(result, indent=2).encode()
            storage().put_object(s.S3_BUCKET, key, io.BytesIO(data), len(data), content_type="application/json")
            conn.execute("UPDATE jobs SET status='complete', result_key=%s, attempts=%s, finished_at=now(), error=NULL WHERE id=%s", (key, attempt, row["id"]))
            logging.info("Completed job %s", row["id"])
        except Exception:
            logging.exception("Job %s attempt %s failed", row["id"], attempt)
            conn.execute("UPDATE jobs SET attempts=%s,status=%s,error='Report processing failed' WHERE id=%s", (attempt, "failed" if attempt >= 3 else "queued", row["id"]))
            # Retry delay prevents hammering unavailable storage.
            time.sleep(2)
    return True

def main():
    logging.info("Worker ready")
    while not stop:
        try:
            if process_one():
                continue
            try:
                redis.blpop("jobs:wakeup", timeout=2)
            except Exception:
                time.sleep(2)
        except Exception:
            logging.exception("Worker infrastructure error; retrying")
            time.sleep(2)
    logging.info("Worker stopped")

if __name__ == "__main__":
    main()
