import time
from app.migrate import main
from app.services import storage
from app import settings as s

if __name__ == "__main__":
    if s.ENVIRONMENT != "local":
        raise RuntimeError("Local bucket bootstrap is not a cloud provisioning command")
    main()
    client = storage()
    for attempt in range(30):
        try:
            if not client.bucket_exists(s.S3_BUCKET):
                client.make_bucket(s.S3_BUCKET)
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
    print("Private local report bucket ready", flush=True)
