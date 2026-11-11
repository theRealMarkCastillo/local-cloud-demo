# Sample MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `mcp` Compose service exposing `submit_job`, `list_jobs` and `get_report` to MCP clients, authorized by Keycloak tokens issued for `${PUBLIC_URL}/mcp`.

**Architecture:** Same backend image, new command `uvicorn app.mcp_server:app`. nginx routes `/mcp` and `/.well-known/oauth-protected-resource/mcp` to it. Job SQL and the owner hash move into shared modules used by the browser API and the MCP service. Keycloak gets an `mcp:tools` scope with an Audience mapper and a public PKCE client `workbench-mcp`.

**Tech Stack:** Python 3.12, FastAPI (existing), MCP Python SDK `mcp==2.3.0` (`MCPServer`, `TokenVerifier`, `AuthSettings`, `TransportSecuritySettings`), PyJWT (pulled in by `mcp`), Keycloak 26.8.1, nginx, stdlib-only smoke test.

**Spec:** `docs/superpowers/specs/2026-11-10-mcp-server-design.md`

## Global Constraints

- Use `backend/Dockerfile` for the new service; no new image or Dockerfile.
- No new host ports. Only nginx stays published on loopback.
- Resource URL is exactly `PUBLIC_URL + "/mcp"`; required scope is exactly `mcp:tools`; client id is exactly `workbench-mcp`.
- Owner is `sha256((OIDC_ISSUER_URL + "|" + sub).encode()).hexdigest()` — identical to today's `main.py`.
- Tool limits: title 1–120 chars, text 1–20000 chars, `list_jobs` returns newest 50.
- MCP service receives no `OIDC_CLIENT_SECRET`; never log token values.
- Worker code unchanged. Browser API behavior unchanged (`make smoke` existing checks must still pass).
- No anonymous DCR. CIMD/resource-indicators are documentation only.
- Commits: work on branch `mcp-server` (create from `main`); run commit steps only after the user approves committing. No AI attribution in messages.
- Validation: `docker compose config --quiet`, `make up`, `make test`, `make smoke`.

## Review Focus

- Keycloak restarts or rotates keys → a token with an unknown `kid` triggers one JWKS refetch, then validates (Task 2 test `test_unknown_kid_refetches_once`).
- Garbage, non-JWT or `alg: none` bearer values → 401, never a 500 (Task 2 tests `test_garbage_token_rejected`, `test_alg_none_rejected`).
- `get_report` with a malformed id like `not-a-uuid` → tool error "Report not found", not a server error (Task 3 smoke check).
- Existing Keycloak volume from before this change → `configure_mcp.py` adds scope/client; a second run changes nothing and does not fail (Task 4 step).
- `PUBLIC_URL` changed via `make tunnel-set` → audience mapper and allowed Host/Origin follow the new origin after services restart (Task 4 `set_public_url.py` change; Task 5 settings derive from `PUBLIC_URL`).

---

### Task 1: Shared owner and job modules

**Files:**
- Create: `backend/app/identity.py`, `backend/app/jobs.py`
- Modify: `backend/app/main.py` (callback owner hash; `submit`, `jobs`, `report` bodies)
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `identity.owner_id(issuer: str, subject: str) -> str`
  - `jobs.create_job(owner: str, title: str, text: str) -> str` (returns job id; inserts row, best-effort Redis wake-up exactly as `main.submit` does today)
  - `jobs.list_jobs(owner: str) -> list[dict]` (same SELECT, LIMIT 50)
  - `jobs.read_report(owner: str, job_id: uuid.UUID) -> bytes | None` (None when not owned/not complete)

- [ ] **Step 1: Write failing tests** in `backend/tests/test_jobs.py`:
  - `test_owner_id_matches_previous_formula`: `owner_id("https://i.example/realms/r", "abc") == hashlib.sha256(b"https://i.example/realms/r|abc").hexdigest()`
  - `test_owner_id_differs_by_issuer`: same subject, different issuer → different values.
  - `test_read_report_unknown_job_returns_none` (local only, `skipUnless(ENVIRONMENT=='local')` like `test_sessions.py`): `read_report("synthetic-owner", uuid.uuid4()) is None`.
- [ ] **Step 2: Run** `make test` (stack must be up: `make up`). Expected: ImportError for `app.identity` / `app.jobs`.
- [ ] **Step 3: Implement** the two modules; move the SQL/MinIO code out of `main.py`; `main.py` calls them and keeps HTTP concerns (404 on `None`, response headers).
- [ ] **Step 4: Run** `docker compose up -d --build api worker && make test && make smoke`. Expected: all tests pass; existing smoke prints all five PASS lines.
- [ ] **Step 5: Commit** `git add backend/app/identity.py backend/app/jobs.py backend/app/main.py backend/tests/test_jobs.py && git commit -m "Move job queries and owner hash into shared modules"`

### Task 2: Dependency and token verifier

**Files:**
- Modify: `backend/requirements.txt` (add `mcp>=2.3,<3`), `backend/requirements.lock` (regenerate)
- Modify: `backend/app/settings.py` (add `MCP_RESOURCE_URL = PUBLIC_URL + "/mcp"`, `MCP_SCOPE = "mcp:tools"`)
- Create: `backend/app/mcp_auth.py`
- Test: `backend/tests/test_mcp_auth.py`

**Interfaces:**
- Produces: `class KeycloakTokenVerifier` implementing the SDK `TokenVerifier` protocol:
  - `__init__(self, issuer: str, audience: str, scope: str, jwks_client: jwt.PyJWKClient)`
  - `async verify_token(self, token: str) -> AccessToken | None` — returns `AccessToken(token=token, client_id=claims["azp"], scopes=claims["scope"].split(), expires_at=claims["exp"], resource=audience, subject=claims["sub"], claims={"iss": claims["iss"]})` or `None`.
  - `mcp_auth.from_settings() -> KeycloakTokenVerifier` — discovers `jwks_uri` from `OIDC_INTERNAL_URL + "/.well-known/openid-configuration"`, rewriting the issuer prefix to the internal URL the same way `main.lifespan` does; checks discovery `issuer == OIDC_ISSUER_URL`.

- [ ] **Step 1: Regenerate the lock** in a throwaway container:
  `docker run --rm -v "$PWD/backend:/b" python:3.12-slim sh -c "pip install -q -r /b/requirements.txt && pip freeze > /b/requirements.lock"`
  Expected: lock contains `mcp==2.3.0` and `PyJWT==`; review the diff for unexpected major bumps of existing pins.
- [ ] **Step 2: Write failing tests** in `backend/tests/test_mcp_auth.py`, using an RSA key generated with `cryptography` and a stub JWKS client whose `get_signing_key_from_jwt` returns it (count calls). Constants: `ISS="https://id.example/realms/workbench"`, `AUD="http://localhost:8080/mcp"`, `SCOPE="mcp:tools"`. Tests (each builds a token with `jwt.encode(..., algorithm="RS256", headers={"kid": ...})`):
  - `test_valid_token_accepted`: returns AccessToken with `subject=="user-1"`, `resource==AUD`, `"mcp:tools" in scopes`.
  - `test_aud_list_containing_resource_accepted`: `aud=[AUD, "account"]` accepted.
  - `test_wrong_audience_rejected`: `aud="workbench"` → None.
  - `test_wrong_issuer_rejected` → None.
  - `test_expired_rejected`: `exp` 120s in the past → None.
  - `test_missing_scope_rejected`: `scope="openid profile"` → None.
  - `test_missing_sub_rejected` → None.
  - `test_bad_signature_rejected`: signed by a different key → None.
  - `test_alg_none_rejected`: `jwt.encode(claims, None, algorithm="none")` → None.
  - `test_garbage_token_rejected`: `"not-a-jwt"` → None.
  - `test_unknown_kid_refetches_once`: stub raises `PyJWKClientError` on first lookup, succeeds on second → accepted; lookup count is 2.
- [ ] **Step 3: Run** `docker compose build api && make test`. Expected: ImportError for `app.mcp_auth`.
- [ ] **Step 4: Implement** `verify_token`: run key lookup + `jwt.decode` in `anyio.to_thread.run_sync`; `algorithms=["RS256","RS384","RS512","ES256","ES384","PS256"]`, `audience=AUD`, `issuer=ISS`, `leeway=30`, `options={"require": ["exp","iss","aud","sub"]}`; on `PyJWKClientError` call `jwks_client.get_signing_key_from_jwt` exactly once more (PyJWKClient already refetches the key set on an unknown `kid`; the retry covers a transient fetch failure), then give up; any `jwt.PyJWTError`/`ValueError`/missing scope → `None`. No logging of the token.
- [ ] **Step 5: Run** `make test`. Expected: all pass.
- [ ] **Step 6: Commit** `git commit -m "Add Keycloak bearer token verifier for MCP"` (files above).

### Task 3: Smoke-test MCP section (failing first)

**Files:**
- Modify: `scripts/smoke.py` (append an MCP section after existing checks; reuse `client()`, `TLS`, `BASE`)

**Interfaces:**
- Consumes: endpoints from Tasks 4–5 (`/mcp`, `/.well-known/oauth-protected-resource/mcp`, Keycloak `workbench-mcp`).
- Produces (inside smoke.py): `mcp_token(username: str, scope: str = "openid mcp:tools") -> str`, `mcp_call(token: str, method: str, params: dict, expected: int = 200, origin: str | None = None) -> dict`.

- [ ] **Step 1: Write the checks**, printing a PASS line after each group:
  1. `POST /mcp` with no token → 401; `WWW-Authenticate` contains `resource_metadata="` + `BASE + "/.well-known/oauth-protected-resource/mcp"`.
  2. `GET /.well-known/oauth-protected-resource/mcp` → `resource == BASE + "/mcp"`; `authorization_servers[0] == BASE + "/identity/realms/workbench"`. Fetch `<issuer>/.well-known/openid-configuration` for `authorization_endpoint`/`token_endpoint`; assert `"S256" in code_challenge_methods_supported`.
  3. `mcp_token`: PKCE S256 verifier (`secrets.token_urlsafe(64)`), `state`, redirect URI `http://127.0.0.1:53682/callback`, params include `resource=BASE+"/mcp"`. Fresh opener whose redirect handler stops when the Location starts with the redirect URI and records it. If the response is the `kc-form-login` page, post credentials (same as `login()`). Assert returned `state` matches; exchange code at the token endpoint (`grant_type=authorization_code`, `client_id=workbench-mcp`, `code_verifier`, `redirect_uri`).
  4. `mcp_call` posts JSON-RPC with headers `Authorization: Bearer`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version: 2025-11-25`. Sequence for `demo`: `initialize` (`protocolVersion` `2025-11-25`, `capabilities {}`, `clientInfo {name: "smoke", version: "1"}`) → result has `serverInfo`; `tools/list` names == `{"submit_job","list_jobs","get_report"}`; `tools/call submit_job` (`title "MCP smoke"`, `text "portable portable worker"`) → `structuredContent.id`; poll `list_jobs` up to 30s until `complete`; `get_report` → `structuredContent.words == 3`.
  5. Isolation: token for `demo-other`; `list_jobs` lacks the id; `get_report` on it → `isError is True`.
  6. Malformed id: `get_report` with `"not-a-uuid"` → `isError is True` (HTTP 200).
  7. Audience: token from `mcp_token("demo", scope="openid")` → `tools/list` returns 401.
  8. Origin: valid token with `Origin: https://evil.example` → 403.
- [ ] **Step 2: Run** `make smoke`. Expected: existing PASS lines, then failure at check 1 (404 from nginx for `/mcp`).
- [ ] **Step 3:** No commit yet; commit with Task 5.

### Task 4: Keycloak scope and client

**Files:**
- Modify: `identity/workbench-realm.json` (add `clientScopes` entry and `workbench-mcp` client)
- Create: `scripts/configure_mcp.py`
- Modify: `Makefile` (`up` and `dev` run `python3 scripts/configure_mcp.py` after `configure_email.py`), `scripts/set_public_url.py` (run `configure_mcp.py` after updating the `workbench` client)

**Interfaces:**
- Produces: realm objects
  - client scope `mcp:tools` (`protocol: openid-connect`, `include.in.token.scope: true`, `display.on.consent.screen: true`) with protocol mapper `oidc-audience-mapper` named `mcp-audience`, config `included.custom.audience = ${PUBLIC_URL}/mcp`, `access.token.claim: true`, `id.token.claim: false`.
  - client `workbench-mcp`: `publicClient: true`, `standardFlowEnabled: true`, `directAccessGrantsEnabled: false`, `implicitFlowEnabled: false`, `serviceAccountsEnabled: false`, `attributes.pkce.code.challenge.method: S256`, `redirectUris: ["http://127.0.0.1/*", "http://localhost/*"]`, `webOrigins: []`, `optionalClientScopes: ["mcp:tools"]`.
  - `configure_mcp.py` reads `PUBLIC_URL` from `.env` (default `http://localhost:8080`), uses `keycloak_admin.admin()`, creates the scope/mapper/client if missing, otherwise updates the mapper audience and client fields; attaches `mcp:tools` as an optional scope of `workbench-mcp` only. Prints one summary line.

- [ ] **Step 1:** Edit realm JSON and write `configure_mcp.py`.
- [ ] **Step 2: Verify on the existing volume:** `make up` (runs the script), then run `python3 scripts/configure_mcp.py` a second time. Expected: both succeed; second run reports no creation.
- [ ] **Step 3: Verify loopback ports:** run `make smoke`; check 3 must get past the authorization request. If Keycloak rejects `redirect_uri` with port 53682, add `http://127.0.0.1:53682/callback` explicitly to the client in both realm JSON and script, and note it in `docs/URLS_AND_CALLBACKS.md`. Expected after this task: smoke still fails, now at check 1 (no `/mcp` route yet).
- [ ] **Step 4: Verify a fresh import** parses: `python3 -m json.tool identity/workbench-realm.json >/dev/null`.
- [ ] **Step 5: Commit** `git commit -m "Add Keycloak MCP scope and public PKCE client"` (realm JSON, script, Makefile, set_public_url.py).

### Task 5: MCP service, Compose and nginx

**Files:**
- Create: `backend/app/mcp_server.py`
- Modify: `compose.yaml` (new `mcp` service), `compose.dev.yaml` (mount + `--reload` for `mcp`), `web/default.conf.template` (routes), `compose.yaml` `web.environment` (`MCP_UPSTREAM: mcp:8000`), `web.depends_on` (`mcp: service_healthy`)

**Interfaces:**
- Consumes: `jobs.*`, `identity.owner_id` (Task 1); `mcp_auth.from_settings`, `settings.MCP_RESOURCE_URL`, `settings.MCP_SCOPE` (Task 2).
- Produces: module-level `app` (Starlette ASGI app) in `app.mcp_server`.

- [ ] **Step 1: Implement `mcp_server.py`:**
  - `server = MCPServer(name="workbench", token_verifier=<from_settings()>, auth=AuthSettings(issuer_url=s.OIDC_ISSUER_URL, resource_server_url=s.MCP_RESOURCE_URL, required_scopes=[s.MCP_SCOPE], validate_token_resource=True))`. Build the verifier lazily or at import after required settings are checked (`OIDC_ISSUER_URL` required).
  - Tools take the caller from the SDK auth context (`mcp.server.auth.middleware.auth_context.get_access_token()`), owner = `owner_id(s.OIDC_ISSUER_URL, token.subject)`.
  - `submit_job(title: Annotated[str, Field(min_length=1, max_length=120)], text: Annotated[str, Field(min_length=1, max_length=20000)]) -> dict` → `{"id": ...}`.
  - `list_jobs() -> dict` → `{"jobs": [...]}` with ids and datetimes as strings.
  - `get_report(job_id: str) -> dict` → parsed report JSON; invalid UUID or `None` from `read_report` raises the SDK tool error with message `Report not found`.
  - `app = server.streamable_http_app(stateless_http=True, json_response=True, max_request_body_size=65536, transport_security=TransportSecuritySettings(allowed_hosts=[urlparse(PUBLIC_URL).netloc], allowed_origins=[PUBLIC_URL]))` plus a `/health` route (`server.custom_route`) returning `{"status":"ok"}`.
- [ ] **Step 2: Compose:** `mcp` uses `<<: *app`, `command: ['uvicorn','app.mcp_server:app','--host','0.0.0.0','--port','8000','--no-proxy-headers','--no-access-log']`, environment `<<: *app-env` plus `OIDC_ISSUER_URL` and `OIDC_INTERNAL_URL` as in `api` (no client id/secret), `depends_on` init/keycloak/redis like `api`, healthcheck like `api` on `/health`.
- [ ] **Step 3: nginx:** add `set $mcp_upstream "${MCP_UPSTREAM}";` and
  `location ~ ^/(mcp|\.well-known/oauth-protected-resource/mcp)$ { proxy_pass http://$mcp_upstream; proxy_set_header Host $http_host; proxy_set_header X-Forwarded-Proto $scheme; proxy_set_header X-Forwarded-For $remote_addr; proxy_buffering off; }`
  Place it before `location /`.
- [ ] **Step 4: Run** `docker compose config --quiet && make up && make test && make smoke`. Expected: all existing PASS lines plus the MCP PASS lines; no 500s in `docker compose logs mcp`.
- [ ] **Step 5: Commit** `git commit -m "Add MCP service with job and report tools"` (mcp_server.py, compose files, nginx template, smoke.py).

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `docs/STACK_PATTERN.md`, `docs/SECURITY.md`, `docs/CLOUD.md`, `docs/URLS_AND_CALLBACKS.md`, `AGENTS.md` (mention `mcp` service in validation scope only if needed)

- [ ] **Step 1: Write:**
  - README: `mcp` in the service list; what `make smoke` now covers; "Connect an MCP client": server URL `http://localhost:8080/mcp`, client id `workbench-mcp`, scope `mcp:tools`, loopback redirect; note clients without pre-registration support need the CIMD opt-in.
  - STACK_PATTERN: table row "MCP tools | `mcp` service, Keycloak `mcp:tools` audience | resource URL `${PUBLIC_URL}/mcp`; one client per MCP client".
  - SECURITY: audience-bound tokens, no token passthrough, no client secret in `mcp`, Host/Origin checks, no DCR.
  - CLOUD: adding `mcp` as a private service behind web; set `OIDC_ISSUER_URL`; CIMD/resource-indicators opt-in steps (`--features=cimd,resource-indicators`, `client-id-metadata-document` executor with trusted domains and resource allow-list `${PUBLIC_URL}/mcp`); explicitly "not deployed or runtime-tested".
  - URLS_AND_CALLBACKS: resource URL, metadata URL, loopback redirects, `make tunnel-set` reruns `configure_mcp.py`.
- [ ] **Step 2: Run** `make check`. Expected: no output, exit 0.
- [ ] **Step 3: Commit** `git commit -m "Document the MCP service"` (docs, README), plus the spec and this plan.
