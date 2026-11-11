"""Gateway ext-authz service: answer with the caller's stored upstream token. Token values are never logged."""
import logging
import time
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from app import mcp_auth
from app import settings as s

log = logging.getLogger(__name__)
# Connected-account alias -> gateway role the caller must hold.
ALIASES = {"upstream-demo": "jira:use"}
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
SKEW_SECONDS, MAX_TTL_SECONDS = 30, 60
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class NotConnected(Exception):
    """The user has not linked this account, or the stored token can no longer be refreshed."""


class Unavailable(Exception):
    """Keycloak could not answer; the gateway must fail closed."""


class TokenCache:
    """Process-memory cache keyed by (user, alias). Never shared between users or written to Redis."""

    def __init__(self, clock=time.time):
        self._clock, self._items = clock, {}

    def get(self, sub: str, alias: str) -> str | None:
        entry = self._items.get((sub, alias))
        if entry and entry[1] > self._clock():
            return entry[0]
        self._items.pop((sub, alias), None)
        return None

    def evict(self, sub: str, alias: str) -> None:
        self._items.pop((sub, alias), None)

    def put(self, sub: str, alias: str, token: str, expires_at: float) -> None:
        until = min(expires_at - SKEW_SECONDS, self._clock() + MAX_TTL_SECONDS)
        if until > self._clock():
            self._items[(sub, alias)] = (token, until)


class Broker:
    def __init__(self, http: httpx.AsyncClient, token_url: str, broker_url_template: str, client_id: str, client_secret: str):
        self.http, self.token_url, self.broker_url = http, token_url, broker_url_template
        self.auth = (client_id, client_secret)

    async def upstream_token(self, user_token: str, alias: str) -> tuple[str, float]:
        try:
            # Only this client is scope-mapped to broker/read-token, so only this exchange can read stored tokens.
            exchanged = await self.http.post(self.token_url, auth=self.auth, data={
                "grant_type": EXCHANGE, "subject_token": user_token, "subject_token_type": ACCESS_TOKEN,
                "requested_token_type": ACCESS_TOKEN})
            if exchanged.status_code != 200:
                raise Unavailable(f"token exchange returned {exchanged.status_code}")
            stored = await self.http.get(self.broker_url.format(alias=alias),
                                         headers={"Authorization": "Bearer " + exchanged.json()["access_token"]})
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise Unavailable(type(error).__name__) from None
        if stored.status_code in (400, 404):
            raise NotConnected(alias)
        if stored.status_code != 200:
            raise Unavailable(f"broker endpoint returned {stored.status_code}")
        try:
            body = stored.json()
            expires_at = float(body.get("accessTokenExpiration") or time.time() + int(body.get("expires_in") or 0))
            return str(body["access_token"]), expires_at
        except (KeyError, ValueError, TypeError) as error:
            raise Unavailable(type(error).__name__) from None


def from_settings(http: httpx.AsyncClient) -> tuple[mcp_auth.KeycloakTokenVerifier, Broker]:
    issuer, internal = s.required("OIDC_ISSUER_URL").rstrip("/"), s.OIDC_INTERNAL_URL
    metadata = httpx.get(internal + "/.well-known/openid-configuration", timeout=15).raise_for_status().json()
    if metadata["issuer"] != issuer:
        raise RuntimeError("OIDC discovery issuer mismatch")
    def local(url):
        return url.replace(issuer, internal, 1)
    verifier = mcp_auth.KeycloakTokenVerifier(issuer, s.GATEWAY_AUDIENCE, None, mcp_auth.jwks_client(local(metadata["jwks_uri"])))
    broker = Broker(http, local(metadata["token_endpoint"]), internal + "/broker/{alias}/token",
                    s.CREDENTIALS_CLIENT_ID, s.required("CREDENTIALS_CLIENT_SECRET"))
    return verifier, broker


def create_app(verifier=None, broker=None, cache=None, public_url=None) -> FastAPI:
    state = {"verifier": verifier, "broker": broker, "cache": cache or TokenCache(),
             "public_url": public_url or s.PUBLIC_URL}

    @asynccontextmanager
    async def lifespan(app):
        if state["verifier"] is None:
            async with httpx.AsyncClient(timeout=10) as http:
                state["verifier"], state["broker"] = from_settings(http)
                yield
        else:
            yield

    app = FastAPI(title="Gateway credentials", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    async def caller(request: Request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        claims = await state["verifier"].claims(token) if scheme.lower() == "bearer" and token else None
        return token, claims

    def unauthorized():
        return JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    @app.post("/evict/{alias}", status_code=204)
    async def evict(alias: str, request: Request):
        """Forget the caller's cached token, so a disconnect takes effect at once instead of after the TTL."""
        if alias not in ALIASES:
            return JSONResponse({"error": "unknown_service"}, status_code=404)
        _, claims = await caller(request)
        if claims is None:
            return unauthorized()
        state["cache"].evict(str(claims["sub"]), alias)
        log.info("credentials sub=%s alias=%s outcome=evicted", claims["sub"], alias)
        return Response(status_code=204)

    @app.api_route("/check/{alias}", methods=METHODS)
    async def check(alias: str, request: Request):
        role = ALIASES.get(alias)
        if role is None:
            return JSONResponse({"error": "unknown_service"}, status_code=404)
        token, claims = await caller(request)
        if claims is None:
            return unauthorized()
        sub = str(claims["sub"])
        roles = claims.get("resource_access", {}).get(s.GATEWAY_CLIENT, {}).get("roles", [])
        if role not in roles:
            log.info("credentials sub=%s alias=%s outcome=forbidden", sub, alias)
            return JSONResponse({"error": "forbidden", "required_role": role}, status_code=403)
        upstream = state["cache"].get(sub, alias)
        outcome = "cache"
        if upstream is None:
            try:
                upstream, expires_at = await state["broker"].upstream_token(token, alias)
            except NotConnected:
                log.info("credentials sub=%s alias=%s outcome=not_connected", sub, alias)
                return JSONResponse({"error": "account_not_connected",
                                     "connect_url": f"{state['public_url']}/connections.html?service={alias}"},
                                    status_code=403)
            except Unavailable as error:
                log.warning("credentials sub=%s alias=%s outcome=unavailable reason=%s", sub, alias, error)
                return JSONResponse({"error": "unavailable"}, status_code=503)
            state["cache"].put(sub, alias, upstream, expires_at)
            outcome = "fetched"
        log.info("credentials sub=%s alias=%s outcome=%s", sub, alias, outcome)
        return Response(status_code=200, headers={"Authorization": "Bearer " + upstream})

    return app


app = create_app()
