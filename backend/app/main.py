import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from app import settings as s
from app import agent, connections, jobs, user_tokens
from app.identity import owner_id
from app.services import db, redis
from app.session import ServerSessionMiddleware

log = logging.getLogger(__name__)
oauth = OAuth()

@asynccontextmanager
async def lifespan(app):
    for name in ("OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        s.required(name)
    # Discover generic OIDC metadata. Local Keycloak has separate browser/server addresses.
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(s.OIDC_INTERNAL_URL + "/.well-known/openid-configuration")
        response.raise_for_status()
    metadata = response.json()
    if metadata["issuer"] != s.OIDC_ISSUER_URL:
        raise RuntimeError("OIDC discovery issuer mismatch")
    if s.OIDC_INTERNAL_URL != s.OIDC_ISSUER_URL:
        for name in ("token_endpoint", "jwks_uri", "userinfo_endpoint"):
            if name in metadata:
                metadata[name] = metadata[name].replace(s.OIDC_ISSUER_URL, s.OIDC_INTERNAL_URL, 1)
    oauth.register("identity", client_id=s.OIDC_CLIENT_ID, client_secret=s.OIDC_CLIENT_SECRET,
                   client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
                   **metadata)
    yield

app = FastAPI(title="Local to Cloud Scaffold", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(ServerSessionMiddleware)

@app.exception_handler(user_tokens.SignInRequired)
async def sign_in_required(request: Request, error: user_tokens.SignInRequired):
    return JSONResponse({"detail": "Sign in again"}, status_code=401)

def user(request: Request):
    value = request.session.get("user")
    if not value:
        raise HTTPException(401, "Sign in first")
    return value

def csrf(request: Request, identity=Depends(user)):
    if request.headers.get("origin") != s.PUBLIC_URL:
        raise HTTPException(403, "Origin mismatch")
    expected = request.session.get("csrf", "")
    actual = request.headers.get("x-csrf-token", "")
    if not expected or not secrets.compare_digest(expected, actual):
        raise HTTPException(403, "CSRF token mismatch")
    return identity

@app.get("/health")
def health():
    with db() as conn:
        conn.execute("SELECT 1")
    redis.ping()
    return {"status": "ok", "environment": s.ENVIRONMENT}

@app.get("/api/config")
def config():
    return {"public_url": s.PUBLIC_URL}

@app.get("/auth/login")
def login():
    # Begin OAuth only on the configured origin so state and callback cookies agree.
    return RedirectResponse(s.PUBLIC_URL + "/auth/start", status_code=303)

@app.get("/auth/start")
async def start_login(request: Request):
    return await oauth.identity.authorize_redirect(request, s.PUBLIC_URL + "/auth/callback")

@app.get("/auth/callback")
async def callback(request: Request):
    try:
        token = await oauth.identity.authorize_access_token(request)
        claims = token.get("userinfo")
        if not claims or not claims.get("sub"):
            raise ValueError("Missing OIDC subject")
    except (OAuthError, ValueError):
        request.session.clear()
        raise HTTPException(400, "Login failed; start a new sign-in")
    # Authlib verifies ID-token signature, issuer, audience, expiry, nonce, and OAuth state.
    # Identity is issuer + subject, never a mutable email address.
    owner = owner_id(s.OIDC_ISSUER_URL, claims["sub"])
    request.session.clear()
    request.session.update(user={"id": owner, "name": claims.get("name") or claims.get("preferred_username") or "Developer"},
                           csrf=secrets.token_urlsafe(32))
    # Kept server-side only: the in-stack agent and the Connections page act with the user's own token.
    user_tokens.save(request.session, token, time.time())
    request.scope["rotate_session"] = True
    return RedirectResponse("/", status_code=303)

@app.get("/api/me")
def me(request: Request, identity=Depends(user)):
    return {"user": identity, "csrf": request.session["csrf"]}

@app.post("/auth/logout")
def logout(request: Request, identity=Depends(csrf)):
    request.session.clear()
    return {"ok": True}  # App logout only; identity-provider SSO session remains.

async def refresh_tokens(refresh_token: str) -> dict:
    return await oauth.identity.fetch_access_token(grant_type="refresh_token", refresh_token=refresh_token)

async def keycloak_token(request: Request) -> str:
    return await user_tokens.access_token(request.session, refresh_tokens, time.time())

def known_alias(alias: str) -> str:
    if alias not in connections.ALIASES:
        raise HTTPException(404, "Unknown service")
    return alias

async def account_api(request: Request, method: str, path: str) -> httpx.Response:
    token = await keycloak_token(request)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(method, s.ACCOUNT_URL + path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json"})
    if response.status_code >= 400:
        log.warning("Keycloak account API %s %s returned %s", method, path, response.status_code)
        raise HTTPException(502, "Identity provider request failed")
    return response

@app.get("/api/connections")
async def list_connections(request: Request, identity=Depends(user)):
    linked = (await account_api(request, "GET", "/linked-accounts")).json()
    return {"connections": connections.statuses(linked)}

@app.post("/api/connections/{alias}/connect")
def connect(alias: str = Depends(known_alias), identity=Depends(csrf)):
    # The browser follows this itself: account linking is an interactive redirect through Keycloak.
    return {"redirect": f"{s.PUBLIC_URL}/auth/link-start/{alias}"}

@app.get("/auth/link-start/{alias}")
async def link_start(request: Request, alias: str = Depends(known_alias), identity=Depends(user)):
    return await oauth.identity.authorize_redirect(request, s.PUBLIC_URL + "/auth/link-callback",
                                                   **connections.link_params(alias))

@app.get("/auth/link-callback")
async def link_callback(request: Request, identity=Depends(user)):
    try:
        token = await oauth.identity.authorize_access_token(request)
        claims = token.get("userinfo") or {}
        if owner_id(s.OIDC_ISSUER_URL, claims.get("sub", "")) != identity["id"]:
            raise ValueError("Linking returned a different user")
    except (OAuthError, ValueError):
        raise HTTPException(400, "Linking failed; start again from Connections")
    user_tokens.save(request.session, token, time.time())
    status = request.query_params.get("kc_action_status", "")
    return RedirectResponse("/connections.html?status=" + (status if status in ("success", "cancelled") else "error"),
                            status_code=303)

@app.delete("/api/connections/{alias}")
async def disconnect(request: Request, alias: str = Depends(known_alias), identity=Depends(csrf)):
    # Keycloak drops the stored upstream token together with the link.
    await account_api(request, "DELETE", "/linked-accounts/" + alias)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await agent.evict_credentials(await keycloak_token(request), alias, client)
    except (httpx.HTTPError, user_tokens.SignInRequired):
        # The link is gone either way; the cached copy expires within a minute on its own.
        log.warning("Could not evict the cached upstream token after disconnect")
    return {"ok": True}

@app.post("/api/agent/run")
async def run_agent(request: Request, identity=Depends(csrf)):
    token = await keycloak_token(request)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            return await agent.run(token, client)
    except httpx.HTTPError:
        raise HTTPException(502, "The gateway could not be reached")

class NewJob(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=20000)

@app.post("/api/jobs", status_code=201)
def submit(payload: NewJob, identity=Depends(csrf)):
    return {"id": jobs.create_job(identity["id"], payload.title, payload.text)}

@app.get("/api/jobs")
def list_jobs(identity=Depends(user)):
    return {"jobs": jobs.list_jobs(identity["id"])}

@app.get("/api/jobs/{job_id}/report")
def report(job_id: uuid.UUID, identity=Depends(user)):
    data = jobs.read_report(identity["id"], job_id)
    if data is None:
        raise HTTPException(404, "Report not found")
    return Response(data, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="report-{job_id}.json"'})
