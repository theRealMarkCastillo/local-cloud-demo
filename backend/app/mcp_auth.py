"""Validate Keycloak-issued bearer tokens for the MCP resource server. Token values are never logged."""
import logging
import anyio
import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from app import settings as s

log = logging.getLogger(__name__)
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
LEEWAY_SECONDS = 30


class KeycloakTokenVerifier:
    def __init__(self, issuer: str, audience: str, scope: str, jwks_client):
        self.issuer, self.audience, self.scope, self.jwks = issuer, audience, scope, jwks_client

    def _signing_key(self, token: str):
        try:
            return self.jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientError:
            # PyJWKClient already refetches on an unknown kid; one retry covers a transient fetch failure.
            return self.jwks.get_signing_key_from_jwt(token)

    def _decode(self, token: str) -> dict:
        key = self._signing_key(token)
        return jwt.decode(token, key, algorithms=ALGORITHMS, audience=self.audience, issuer=self.issuer,
                          leeway=LEEWAY_SECONDS, options={"require": ["exp", "iss", "aud", "sub"]})

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self._decode, token)
        except (jwt.PyJWTError, ValueError, TypeError):
            return None
        scopes = str(claims.get("scope", "")).split()
        if self.scope not in scopes or not claims.get("sub"):
            return None
        return AccessToken(token=token, client_id=str(claims.get("azp", "")), scopes=scopes,
                           expires_at=int(claims["exp"]), resource=self.audience,
                           subject=str(claims["sub"]), claims={"iss": claims["iss"]})


def jwks_client(uri: str) -> jwt.PyJWKClient:
    # Cache the key set for 5 minutes only. No per-kid cache: it never expires, so keys
    # removed from Keycloak would stay trusted until restart.
    return jwt.PyJWKClient(uri, cache_jwk_set=True, lifespan=300, cache_keys=False, timeout=10)


def from_settings() -> KeycloakTokenVerifier:
    """Discover the JWKS URL on the internal issuer address, mirroring the browser API."""
    issuer, internal = s.required("OIDC_ISSUER_URL").rstrip("/"), s.OIDC_INTERNAL_URL
    response = httpx.get(internal + "/.well-known/openid-configuration", timeout=15)
    response.raise_for_status()
    metadata = response.json()
    if metadata["issuer"] != issuer:
        raise RuntimeError("OIDC discovery issuer mismatch")
    jwks_uri = metadata["jwks_uri"].replace(issuer, internal, 1)
    return KeycloakTokenVerifier(issuer, s.MCP_RESOURCE_URL, s.MCP_SCOPE, jwks_client(jwks_uri))
