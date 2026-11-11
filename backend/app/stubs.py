"""Local stand-ins behind the gateway. Never deployed: a fake LLM, a header echo and a Jira-like MCP server."""
import os
from urllib.parse import urlparse
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from app import mcp_auth
from app import settings as s

UPSTREAM_ISSUER = s.PUBLIC_URL + "/identity/realms/upstream-demo"
UPSTREAM_JWKS = os.getenv("UPSTREAM_JWKS_URL", "http://keycloak:8080/identity/realms/upstream-demo/protocol/openid-connect/certs")
AUDIENCE = "jira-stub"
LLM_TOKENS = 500


class UpstreamVerifier:
    """Accept only tokens the stand-in SaaS realm issued for this stub; keep the upstream username."""

    def __init__(self, inner: mcp_auth.KeycloakTokenVerifier):
        self.inner = inner

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = await self.inner.claims(token)
        if claims is None:
            return None
        return AccessToken(token=token, client_id=str(claims.get("azp", "")), scopes=str(claims.get("scope", "")).split(),
                           expires_at=int(claims["exp"]), resource=AUDIENCE, subject=str(claims["sub"]),
                           claims={"preferred_username": str(claims.get("preferred_username") or claims["sub"])})


def default_verifier() -> UpstreamVerifier:
    return UpstreamVerifier(mcp_auth.KeycloakTokenVerifier(UPSTREAM_ISSUER, AUDIENCE, None, mcp_auth.jwks_client(UPSTREAM_JWKS)))


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def chat(request: Request):
    # Fixed usage makes gateway token budgets deterministic; the auth prefix shows whose key arrived.
    seen = request.headers.get("authorization", "")[:15]
    return JSONResponse({
        "id": "stub-completion", "object": "chat.completion", "created": 0, "model": "fake-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "stub reply auth=" + seen}}],
        "usage": {"prompt_tokens": LLM_TOKENS // 2, "completion_tokens": LLM_TOKENS // 2, "total_tokens": LLM_TOKENS}})


async def echo(request: Request):
    return JSONResponse({"path": request.url.path, "headers": {k.lower(): v for k, v in request.headers.items()}})


def whoami() -> dict[str, str]:
    token = get_access_token()
    return {"upstream_user": token.claims["preferred_username"] if token else ""}


def create_app(verifier: UpstreamVerifier | None = None):
    server = MCPServer(
        name="jira-stub",
        instructions="Stand-in for a SaaS MCP server that only trusts its own authorization server.",
        token_verifier=verifier or default_verifier(),
        auth=AuthSettings(issuer_url=UPSTREAM_ISSUER, resource_server_url="http://stubs:8000/jira/mcp"),
    )
    server.tool(description="Return the upstream account this request is authenticated as.")(whoami)
    server.custom_route("/health", methods=["GET"])(health)
    server.custom_route("/v1/chat/completions", methods=["POST"])(chat)
    server.custom_route("/echo/{path:path}", methods=["GET", "POST"])(echo)
    return server.streamable_http_app(
        streamable_http_path="/jira/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(allowed_hosts=["stubs:8000", urlparse(s.PUBLIC_URL).netloc]),
    )


app = create_app()
