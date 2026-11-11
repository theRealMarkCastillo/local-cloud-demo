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
TOKEN_URL = OIDC_INTERNAL_URL + "/protocol/openid-connect/token"
# In-stack agent: its own confidential client, and the internal addresses it calls.
WORKBENCH_AGENT_SECRET = os.getenv("WORKBENCH_AGENT_SECRET", "")
REPORTER_CLIENT_ID = "reporter-agent"
GATEWAY_INTERNAL_URL = os.getenv("GATEWAY_INTERNAL_URL", "http://gateway:3000").rstrip("/")
CREDENTIALS_INTERNAL_URL = os.getenv("CREDENTIALS_INTERNAL_URL", "http://credentials:8000").rstrip("/")
# Keycloak account API (linked accounts), called with the user's own token.
ACCOUNT_URL = OIDC_INTERNAL_URL + "/account"
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
# MCP resource identifier (RFC 8707/9728). Tokens must name it in `aud`.
MCP_RESOURCE_URL = PUBLIC_URL + "/mcp"
MCP_SCOPE = "mcp:tools"
# Outbound gateway: agent tokens name this resource in `aud` (only agent clients map it) and carry
# roles of the `gateway` client; the credentials service exchanges them.
GATEWAY_AUDIENCE = PUBLIC_URL + "/gateway"
GATEWAY_CLIENT = "gateway"
CREDENTIALS_CLIENT_ID = "gateway-credentials"
CREDENTIALS_CLIENT_SECRET = os.getenv("CREDENTIALS_CLIENT_SECRET", "")
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

# Empty host disables app email outside local Compose. Cloud credentials stay server-side.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "workbench@example.test")
if SMTP_HOST:
    if not 1 <= SMTP_PORT <= 65535 or SMTP_SECURITY not in ("none", "starttls", "tls"):
        raise RuntimeError("Invalid SMTP port/security configuration")
    if bool(SMTP_USERNAME) != bool(SMTP_PASSWORD):
        raise RuntimeError("SMTP username/password must be configured together")
    if ENVIRONMENT != "local" and SMTP_SECURITY == "none":
        raise RuntimeError("Cloud SMTP must use starttls or tls")
