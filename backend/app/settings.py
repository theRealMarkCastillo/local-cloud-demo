import os
from urllib.parse import urlparse, parse_qs

def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing {name}")
    return value

ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
PUBLIC_URL = required("PUBLIC_URL").rstrip("/")
DATABASE_URL = required("DATABASE_URL")
REDIS_URL = required("REDIS_URL")
OIDC_ISSUER_URL = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
OIDC_INTERNAL_URL = os.getenv("OIDC_INTERNAL_URL", OIDC_ISSUER_URL).rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
S3_ENDPOINT = required("S3_ENDPOINT")  # URL, e.g. http://minio:9000
S3_ACCESS_KEY = required("S3_ACCESS_KEY")
S3_SECRET_KEY = required("S3_SECRET_KEY")
S3_BUCKET = required("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
COOKIE_SECURE = ENVIRONMENT != "local" or PUBLIC_URL.startswith("https://")
COOKIE_NAME = "__Host-demo-session" if COOKIE_SECURE else "demo-session"
SESSION_TTL = 3600
if ENVIRONMENT != "local":
    for name, value in (("PUBLIC_URL", PUBLIC_URL), ("OIDC_ISSUER_URL", OIDC_ISSUER_URL),
                        ("OIDC_INTERNAL_URL", OIDC_INTERNAL_URL), ("S3_ENDPOINT", S3_ENDPOINT)):
        if value and urlparse(value).scheme != "https":
            raise RuntimeError(f"{name} must use HTTPS outside local development")
    mode = parse_qs(urlparse(DATABASE_URL).query).get("sslmode", [""])[0]
    if mode not in ("verify-full", "require"):
        raise RuntimeError("Cloud DATABASE_URL must use sslmode=verify-full or require")
    if not REDIS_URL.startswith("rediss://"):
        raise RuntimeError("Cloud REDIS_URL must use rediss://")
    if "local-only" in OIDC_CLIENT_SECRET or S3_ACCESS_KEY == "localminio":
        raise RuntimeError("Replace local demo credentials before deploying")
