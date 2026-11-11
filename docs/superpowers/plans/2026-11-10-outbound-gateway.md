# Outbound Agent Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an agentgateway-based outbound gateway, a `credentials` helper and local stand-ins so agents reach our MCP server (OBO), a per-user SaaS stand-in (connected account), a shared-key service and an LLM, governed by Keycloak roles and proven by `make smoke`.

**Architecture:** agentgateway v1.7.0 sits behind nginx at `/gateway/*`, validates agent tokens and applies one credential strategy per route. A FastAPI `credentials` service answers agentgateway's HTTP ext-authz call with the user's stored Keycloak broker token. The API stores user tokens server-side, offers a Connections flow and a scripted in-stack agent.

**Tech Stack:** agentgateway v1.7.0, Keycloak 26.8.1 (standard token exchange v2, link-only identity provider), FastAPI, httpx, PyJWT, Redis server-side sessions, nginx, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-10-outbound-gateway-design.md`

## Global Constraints

- Validate with `docker compose config --quiet`, `make up`, `make smoke`; unit tests with `make test` and `(cd scripts && python3 -m unittest discover -p 'test_*.py')`.
- No paid cloud resources; cloud docs say "not deployed".
- `.env*` stays ignored; every new secret has a `local-only-…` placeholder in `.env.example` and a matching `${VAR:-local-only-…}` default in Compose.
- Keep owner-scoped job access, OIDC validation, server-side sessions, CSRF checks; the existing 11 smoke checks keep passing.
- Our services use `backend/Dockerfile`; gateway image `ghcr.io/agentgateway/agentgateway:v1.7.0`.
- No new host ports. Only nginx `/gateway/` reaches the gateway; the gateway admin port and `credentials` are never published.
- Upstream tokens never logged, never in Redis, never returned to agents or browsers.
- Names fixed by the spec: clients `gateway`, `gateway-credentials`, `workbench-agent`, `agent-cli`, `agent-cli-writer`; roles on `gateway`: `llm:use`, `workbench:read`, `workbench:write`, `jira:use`, `echo:use`; groups `gateway-users`, `gateway-jira`; IdP alias and realm `upstream-demo`; routes `/gateway/mcp/workbench`, `/gateway/mcp/jira`, `/gateway/echo`, `/gateway/llm/v1/*`.
- Cache TTL min(upstream expiry − 30 s, 60 s); LLM budget 1000 tokens/h keyed by `jwt.sub`; stub LLM reports 500 tokens per call.
- Commit messages: concise, no AI attribution trailers.

## Review Focus

1. Two users calling the Jira route at the same moment must never receive each other's cached upstream token (cache key is (`sub`, alias), not the bearer token prefix or the agent).
2. A refresh-token failure in the in-stack agent (expired or revoked session at Keycloak) must clear stored tokens and return 401, not loop or 500.
3. Logout must delete the stored Keycloak tokens from the server-side session even when the session is then revoked.
4. Rerunning `scripts/set_public_url.py` with a tunnel URL must update the IdP's browser-facing URLs and agent redirect URIs; stale `localhost` URLs break linking.
5. Upstream tokens and refresh tokens must not appear in `credentials` logs, including on errors.

---

### Task 1: Keycloak model and stand-in upstream realm

**Files:**
- Create: `identity/upstream-demo-realm.json`
- Create: `scripts/configure_gateway.py`
- Create: `scripts/test_configure_gateway.py`
- Modify: `compose.yaml` (keycloak: mount the new realm file into `/opt/keycloak/data/import/`, env `UPSTREAM_BROKER_SECRET`)
- Modify: `Makefile` (`up`, `dev` run `python3 scripts/configure_gateway.py` after `configure_mcp.py`), `scripts/set_public_url.py` (rerun it)
- Modify: `.env.example` (add `GATEWAY_CLIENT_SECRET`, `CREDENTIALS_CLIENT_SECRET`, `WORKBENCH_AGENT_SECRET`, `UPSTREAM_BROKER_SECRET`, `GATEWAY_ECHO_KEY`, `GATEWAY_LLM_KEY`)

**Interfaces:**
- Produces: `configure_gateway.desired(public_url: str, secrets: dict[str, str]) -> dict` with keys `clients` (list of client representations), `roles` (list of str), `groups` (dict group → list of role names), `scope_mappings` (dict clientId → list of gateway roles), `idp` (identity-provider representation). `configure_gateway.env(name: str, default: str) -> str` (environment, then `.env`, quotes stripped; same precedence as `configure_mcp.public_url`). `main()` applies `desired()` idempotently and prints `created: …` or `nothing (already present)`.
- Realm `upstream-demo`: client `workbench-broker` (confidential, secret `${UPSTREAM_BROKER_SECRET}`, redirect `${PUBLIC_URL}/identity/realms/workbench/broker/upstream-demo/endpoint*`), user `demo-upstream` / `${DEMO_PASSWORD}`, access token lifespan 300 s, and on `workbench-broker` an audience mapper `included.custom.audience = jira-stub`. Omit `clientScopes` from the import so Keycloak keeps its built-in scopes (the spike's realm lacked `sub` because importing `clientScopes` replaced them); `configure_gateway.py` creates optional scope `jira.read` in `upstream-demo` and links it to `workbench-broker`.

- [ ] **Step 1: Write the failing tests** in `scripts/test_configure_gateway.py`:
  - `test_agent_clients_have_no_full_scope`: every client in `desired(...)["clients"]` whose clientId starts with `agent-` or equals `workbench-agent` or `workbench` has `fullScopeAllowed is False`.
  - `test_only_credentials_client_maps_read_token`: in `scope_mappings`, `broker/read-token` appears only under `gateway-credentials`.
  - `test_agent_audiences_include_gateway_and_credentials`: each agent client's `protocolMappers` include audience mappers with `included.client.audience` `gateway` and `gateway-credentials`.
  - `test_workbench_client_adds_workbench_agent_audience`.
  - `test_workbench_redirects_include_link_callback`: `workbench` `redirectUris` == [`<public>/auth/callback`, `<public>/auth/link-callback`].
  - `test_agent_scope_mappings_match_spec`: `agent-cli` → {`llm:use`,`workbench:read`,`jira:use`,`echo:use`}; `agent-cli-writer` → {`workbench:read`,`workbench:write`}; `workbench-agent` → {`llm:use`,`workbench:read`,`jira:use`}.
  - `test_groups_split_jira`: `gateway-users` lacks `jira:use`, contains `broker/read-token`; `gateway-jira` == [`jira:use`].
  - `test_idp_urls_follow_public_url`: with `https://demo.example`, `idp["config"]["authorizationUrl"]` starts with `https://demo.example/identity/realms/upstream-demo/`, `tokenUrl`/`jwksUrl` start with `http://localhost:8080/identity/realms/upstream-demo/`, `linkOnly` and `storeToken` are true, `defaultScope == "openid jira.read"`.
  - `test_env_prefers_environment_then_env_file` (mirror `test_configure_mcp`).

- [ ] **Step 2: Run** `(cd scripts && python3 -m unittest test_configure_gateway -v)`. Expected: FAIL, `No module named 'configure_gateway'`.

- [ ] **Step 3: Implement** `desired()`, `env()`, `main()` in `scripts/configure_gateway.py` using `keycloak_admin.admin()` and the get-or-create/update pattern of `configure_mcp.py`. `main()` also: adds `<public>/auth/link-callback` to the `workbench` client's redirect URIs; makes `demo` a member of both groups and `demo-other` of `gateway-users`; adds realm default-role composite `account/manage-account-links`; sets `standard.token.exchange.enabled=true` on `gateway`, `gateway-credentials`, `workbench-agent`; links optional scope `mcp:tools` to `gateway`; sets `workbench` client `fullScopeAllowed=false` with no role mappings (the app reads no roles today). Secrets come from `env()` with the `.env.example` defaults.

- [ ] **Step 4: Run** the unit tests again. Expected: PASS (9 tests).

- [ ] **Step 5: Apply and verify idempotency.** Run `docker compose config --quiet && make up` (recreates keycloak so the new realm imports), then `python3 scripts/configure_gateway.py` a second time. Expected: second run prints `nothing (already present)`; `make smoke` still prints the existing 11 PASS lines (proves `workbench` without full scope still logs in).

- [ ] **Step 6: Commit**
```bash
git add identity/upstream-demo-realm.json scripts/configure_gateway.py scripts/test_configure_gateway.py compose.yaml Makefile scripts/set_public_url.py .env.example
git commit -m "Add Keycloak gateway clients, roles and upstream-demo provider"
```

### Task 2: Gate — open checks (throwaway probe, nothing committed but a ledger record)

**Files:** scratch only (session scratchpad): a gateway config, a 20-line authz stub, a probe script.

**Interfaces:**
- Consumes: Task 1's Keycloak clients and IdP.
- Produces: ledger lines `Task 2: check N: pass|fail — <evidence>` for N = 1..4, which later tasks read to pick the primary path or the named fallback.

- [ ] **Step 1: Check 1 (ext-authz header + deny body).** Run agentgateway v1.7.0 on the Compose network with a route whose `extAuthz` uses `protocol.http` with `includeResponseHeaders: [authorization]`, pointing at a stub returning 200 + `Authorization: Bearer upstream-x` for one path and 403 + JSON body for another; backend = an echo of request headers. Expected pass: echo sees `Bearer upstream-x`; client receives 403 with the stub's JSON body. Fallback if fail: Task 5 adds `credentials` routes `/proxy/jira/*` that forward to `stubs` itself, and the gateway route uses a plain backend to `credentials` instead of ext-authz.
- [ ] **Step 2: Check 2 (token exchanges).** As demo, PKCE-login `agent-cli`; exchange that token as `gateway-credentials` (no `scope`); call `GET /identity/realms/workbench/broker/upstream-demo/token` with the result. Expected pass: 400 "not associated" (proves the role check passed; a 403 means the exchanged token lacks `read-token`). Then exchange a `workbench` session token (obtained via standard flow with client secret) as `workbench-agent`. Expected pass: `aud` contains `gateway` and `gateway-credentials`, roles = {`llm:use`,`workbench:read`,`jira:use`}. **If either fails: stop and report; no fallback.**
- [ ] **Step 3: Check 3 (LLM under a path prefix).** Route `/gateway/llm/v1/*` with an `ai` backend and a path rewrite stripping `/gateway/llm`. Expected pass: stub sees `/v1/chat/completions`. Fallback: a second listener on internal port 3001, nginx maps `/gateway/llm/` to it.
- [ ] **Step 4: Check 4 (secrets from environment).** Put `clientSecret` and a `backendAuth.key` as environment references in the config (try `$VAR`/`${VAR}` per agentgateway docs). Expected pass: exchange succeeds with the env-provided secret. Fallback: define the gateway config with Compose top-level `configs: gateway-config: content: |` (Compose interpolates `${VAR}`), mounted at `/etc/agentgateway/config.yaml`.
- [ ] **Step 5: Record** the four results and chosen paths as ledger rulings; remove the scratch containers.

### Task 3: `credentials` service

**Files:**
- Modify: `backend/app/mcp_auth.py` (`KeycloakTokenVerifier(issuer, audience, scope, jwks_client)`: `scope=None` skips the scope check)
- Modify: `backend/tests/test_mcp_auth.py` (one test for `scope=None`)
- Modify: `backend/app/settings.py` (`CREDENTIALS_CLIENT_ID = "gateway-credentials"`, `CREDENTIALS_CLIENT_SECRET` from env, `GATEWAY_AUDIENCE = "gateway"`)
- Create: `backend/app/credentials.py`
- Create: `backend/tests/test_credentials.py`

**Interfaces:**
- Consumes: `mcp_auth.KeycloakTokenVerifier`, `mcp_auth.jwks_client`, `mcp_auth.from_settings` discovery pattern.
- Produces:
  - `class NotConnected(Exception)`, `class Unavailable(Exception)`
  - `class TokenCache(clock=time.time)`: `get(sub: str, alias: str) -> str | None`; `put(sub: str, alias: str, token: str, expires_at: float) -> None` storing until `min(expires_at - 30, clock() + 60)`
  - `class Broker(http: httpx.AsyncClient, token_url: str, broker_url_template: str, client_id: str, client_secret: str)`: `async upstream_token(user_token: str, alias: str) -> tuple[str, float]` (exchange as `gateway-credentials`, then GET broker URL; maps 400 "not associated" or refresh failure → `NotConnected`, network errors/5xx → `Unavailable`)
  - FastAPI `app` with `GET /check/{alias}`: verify bearer (audience `gateway`, role `jira:use` in `resource_access.gateway.roles` for alias `upstream-demo`), cache lookup, broker fetch; 200 with header `Authorization: Bearer <token>`; 401 invalid token; 403 `{"error":"forbidden","required_role":"jira:use"}`; 403 `{"error":"account_not_connected","connect_url":PUBLIC_URL+"/connections?service=upstream-demo"}`; 503 on `Unavailable`. Unknown alias → 404. `GET /health`.

- [ ] **Step 1: Write the failing tests** (synthetic keys as in `test_mcp_auth.py`; `httpx.MockTransport` for Keycloak):
  - `test_verifier_without_scope_accepts_token` (in `test_mcp_auth.py`)
  - `test_cache_hit_within_ttl`, `test_cache_ttl_capped_at_60s`, `test_cache_expires_30s_before_upstream`, `test_cache_isolated_by_user_and_alias`
  - `test_check_returns_authorization_header` (exchange + broker mocked, 200, header value)
  - `test_check_uses_cache_second_time` (broker called once for two checks)
  - `test_check_rejects_wrong_audience` (401), `test_check_requires_jira_role` (403 forbidden body)
  - `test_not_linked_maps_to_account_not_connected` (broker 400 → 403 body with `connect_url`)
  - `test_keycloak_down_is_503` (`httpx.ConnectError`)
  - `test_concurrent_users_never_share_tokens` (two different `sub`s, interleaved `asyncio.gather`, each gets its own token) — Review Focus 1
  - `test_tokens_not_logged` (`assertLogs` on `app.credentials` during success and a broker 500; no record contains the upstream access or refresh token) — Review Focus 5
- [ ] **Step 2: Run** `make test`. Expected: FAIL on import of `app.credentials` and the new verifier test.
- [ ] **Step 3: Implement** the verifier change and `backend/app/credentials.py`. Discovery mirrors `mcp_auth.from_settings` (internal URL rewrite). One `httpx.AsyncClient(timeout=10)` per process. Log only `sub`, alias, outcome.
- [ ] **Step 4: Run** `make test`. Expected: PASS, all existing tests still green.
- [ ] **Step 5: Commit** `git commit -m "Add credentials ext-authz service for connected accounts"` (files above).

### Task 4: `stubs` service

**Files:**
- Create: `backend/app/stubs.py`, `backend/tests/test_stubs.py`

**Interfaces:**
- Produces FastAPI `app`:
  - `POST /v1/chat/completions` → OpenAI-shaped body, `usage.total_tokens == 500`, content `"stub reply"`; echoes `authorization` prefix (first 15 chars) in `choices[0].message.content` suffix for smoke assertions.
  - `ANY /echo/{path:path}` → `{"headers": {...lower-cased request headers}}`.
  - `/jira/mcp`: `MCPServer` (stateless, JSON responses, `TransportSecuritySettings(allowed_hosts=["stubs:8000", "localhost:8080", <PUBLIC_URL netloc>])`) with tool `whoami() -> dict` returning `{"upstream_user": preferred_username}`, protected by a `KeycloakTokenVerifier` for issuer `PUBLIC_URL/identity/realms/upstream-demo`, audience `jira-stub` (Task 1's mapper), `scope=None`, JWKS from `http://keycloak:8080/identity/realms/upstream-demo/protocol/openid-connect/certs`.
  - `GET /health`.
- [ ] **Step 1: Write the failing tests:** `test_llm_reports_500_tokens`, `test_echo_returns_headers`, `test_jira_rejects_workbench_realm_token` (token signed with the right key but `iss` of the workbench realm → 401), `test_jira_whoami_returns_upstream_user`.
- [ ] **Step 2: Run** `make test`. Expected: FAIL, missing module.
- [ ] **Step 3: Implement** `backend/app/stubs.py`.
- [ ] **Step 4: Run** `make test`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Add local stand-in LLM, echo and Jira MCP services"`.

### Task 5: Gateway wiring and gateway smoke checks

**Files:**
- Create: `gateway/config.yaml` (or Compose inline config per Task 2 check 4)
- Modify: `compose.yaml` (services `gateway`, `credentials`, `stubs`; `web` env `GATEWAY_UPSTREAM: gateway:3000` and `depends_on`), `compose.dev.yaml` (reload overrides for `credentials`, `stubs`)
- Modify: `web/default.conf.template` (`set $gateway_upstream`; `location /gateway/` → `proxy_pass http://$gateway_upstream;`, `proxy_buffering off`, `Host $http_host`), `web/Dockerfile` (`GATEWAY_UPSTREAM=api:8000` default)
- Modify: `scripts/smoke.py` (gateway section)

**Interfaces:**
- Consumes: Task 1 clients/roles/secrets, Task 2 rulings, Task 3 `GET /check/upstream-demo` on `credentials:8000`, Task 4 stub paths.
- Produces: routes per spec; access log fields `jwt.sub`, `jwt.azp`, `mcp.method.name`, `gen_ai.tool.name`, status (add explicit `azp` log field); smoke helper `pkce_token(client: str, username: str, scope: str = "openid") -> str` (generalize `mcp_token`).

Gateway config essentials (values the implementer cannot infer):
- `jwtAuth`: issuer `${PUBLIC_URL}/identity/realms/workbench`, audiences `[gateway]`, JWKS `http://keycloak:8080/identity/realms/workbench/protocol/openid-connect/certs`.
- Role CEL per route: `'<role>' in jwt.resource_access.gateway.roles` (`workbench:read`, `jira:use`, `echo:use`, `llm:use`); deny → 403 with `{"error":"forbidden","required_role":"<role>"}`.
- `/gateway/mcp/workbench`: `urlRewrite.authority.full` = PUBLIC_URL netloc; MCP target `http://mcp:8000/mcp`; `backendAuth.oauthTokenExchange` (client `gateway`, `clientSecretBasic`, scopes `[mcp:tools]`, subject `jwt.rawToken.unredacted()`); `mcpAuthorization` rules: `mcp.tool.name in ["list_jobs","get_report"] && 'workbench:read' in jwt.resource_access.gateway.roles`, `mcp.tool.name == "submit_job" && 'workbench:write' in jwt.resource_access.gateway.roles`.
- `/gateway/mcp/jira`: `extAuthz` http to `credentials:8000`, path `"/check/upstream-demo"`, `includeRequestHeaders: [authorization]`, `includeResponseHeaders: [authorization]`, `failureMode: {denyWithStatus: 503}`; MCP target `http://stubs:8000/jira/mcp`.
- `/gateway/echo`: `backendAuth.key` = `GATEWAY_ECHO_KEY`; backend `stubs:8000` path `/echo/…`.
- `/gateway/llm/v1/*`: `ai` backend OpenAI, `hostOverride: stubs:8000`, `backendAuth.key` = `GATEWAY_LLM_KEY`, `localRateLimit` tokens 1000/1h key `jwt.sub`.
- Admin/UI listener bound to `127.0.0.1` inside the container; no `ports:` on `gateway`, `credentials`, `stubs`.

- [ ] **Step 1: Write the failing smoke checks** (append; each prints one PASS line):
  1. `PASS: gateway rejects missing and wrong-audience tokens` (no token 401; `mcp_token("demo")` 401)
  2. `PASS: gateway governance is user ∩ agent` (`agent-cli` `tools/list` == [`get_report`,`list_jobs`]; `submit_job` error; `agent-cli-writer` `submit_job` succeeds; `demo-other` via `agent-cli` on `/gateway/mcp/jira` → 403 `required_role` `jira:use`)
  3. `PASS: OBO through the gateway stays owner-scoped` (demo and demo-other `list_jobs` ids disjoint, both non-empty after each submits via the browser API)
  4. `PASS: shared-key route hides the user token` (echo `authorization` == `Bearer ` + local echo key)
  5. `PASS: LLM budget is per user` (demo: 200, 200, 429; demo-other: 200)
  6. `PASS: gateway internals are not reachable from the host` (connections to `127.0.0.1:15000`, `:3000`, `:8000`-style internal ports refused — assert no Compose `ports` for `gateway`, `credentials`, `stubs` via `docker compose config --format json`)
- [ ] **Step 2: Run** `make smoke`. Expected: existing 11 PASS, then failure at check 1 (404 from nginx: no `/gateway/`).
- [ ] **Step 3: Implement** config, Compose services, nginx location, Dockerfile default.
- [ ] **Step 4: Run** `docker compose config --quiet && make up && make smoke`. Expected: 17 PASS lines. Note: LLM check needs a fresh budget — `make up` recreates `gateway`; if rerunning smoke, `docker compose restart gateway` first (document in `docs/GATEWAY.md`).
- [ ] **Step 5: Commit** `git commit -m "Add agentgateway with OBO, shared-key, LLM and Jira routes"`.

### Task 6: Server-side user tokens and Connections API

**Files:**
- Create: `backend/app/user_tokens.py`, `backend/app/connections.py`
- Create: `backend/tests/test_user_tokens.py`, `backend/tests/test_connections.py`
- Modify: `backend/app/main.py` (callback stores tokens; logout clears; new endpoints), `backend/app/settings.py` (`ACCOUNT_URL = OIDC_INTERNAL_URL + "/account"`)

**Interfaces:**
- Produces:
  - `user_tokens.save(session: dict, token: dict, now: float) -> None` → `session["tokens"] = {"access": …, "refresh": …, "expires_at": now + expires_in}`
  - `class SignInRequired(Exception)`
  - `async user_tokens.access_token(session: dict, refresh: Callable[[str], Awaitable[dict]], now: float) -> str` — returns stored access token if it expires in > 30 s, else calls `refresh(refresh_token)`, saves and returns the new one; on refresh error clears `session["tokens"]` and raises `SignInRequired`
  - `connections.ALIASES = ("upstream-demo",)`; `connections.link_params(alias: str) -> dict` → `{"kc_action": "idp_link:<alias>"}` (raises `KeyError` for unknown aliases)
  - Endpoints: `GET /api/connections` (user) → `{"connections":[{"alias":"upstream-demo","connected":bool}]}` via account API `GET /account/linked-accounts`; `POST /api/connections/{alias}/connect` (csrf) → `{"redirect": PUBLIC_URL + "/auth/link-start/<alias>"}`; `GET /auth/link-start/{alias}` (user) → `oauth.identity.authorize_redirect(request, PUBLIC_URL + "/auth/link-callback", **link_params(alias))` so Authlib handles state and PKCE as in `/auth/start`; `GET /auth/link-callback` → `authorize_access_token` (Authlib checks state), saves fresh tokens, redirects to `/connections.html?status=<kc_action_status>`; `DELETE /api/connections/{alias}` (csrf) → account API `DELETE /account/linked-accounts/{alias}`. Unknown alias → 404. `SignInRequired` → 401.
- [ ] **Step 1: Write the failing tests:**
  - `test_save_records_expiry`, `test_access_token_fresh_returns_stored`, `test_access_token_refreshes_near_expiry`, `test_refresh_failure_clears_and_raises` (Review Focus 2)
  - `test_logout_clears_tokens` (TestClient with a seeded session: after `/auth/logout`, `tokens` gone even though the session is revoked — assert via the session dict captured before revocation) (Review Focus 3)
  - `test_link_params_sets_kc_action`, `test_link_params_unknown_alias_raises`
  - `test_connect_requires_csrf` (403 without header), `test_disconnect_requires_csrf`, `test_unknown_alias_404`
  - `test_link_callback_rejects_bad_state` (400)
- [ ] **Step 2: Run** `make test`. Expected: FAIL, missing modules.
- [ ] **Step 3: Implement.** Callback calls `user_tokens.save(request.session, token, time.time())` after the existing `session.update`. Refresh uses `oauth.identity.fetch_access_token(grant_type="refresh_token", refresh_token=…)`.
- [ ] **Step 4: Run** `make test`, then `make up && make smoke`. Expected: unit tests PASS; smoke still 17 PASS.
- [ ] **Step 5: Commit** `git commit -m "Store user tokens server-side and add connections API"`.

### Task 7: In-stack sample agent

**Files:**
- Create: `backend/app/agent.py`, `backend/tests/test_agent.py`
- Modify: `backend/app/main.py` (`POST /api/agent/run`), `backend/app/settings.py` (`WORKBENCH_AGENT_SECRET`, `GATEWAY_INTERNAL_URL = "http://gateway:3000"`), `compose.yaml` (api env `WORKBENCH_AGENT_SECRET`)

**Interfaces:**
- Consumes: `user_tokens.access_token`, gateway routes.
- Produces:
  - `async agent.gateway_token(user_access_token: str, http: httpx.AsyncClient) -> str` — standard token exchange as `workbench-agent` (client secret basic), no `scope`
  - `async agent.run(user_access_token: str, http: httpx.AsyncClient) -> dict` → `{"steps":[{"step":"llm","status":int},{"step":"list_jobs","status":int,"count":int},{"step":"jira","status":int,"detail":str}]}`; a 403 `account_not_connected` from Jira is a step result carrying `connect_url`, not an exception
  - `POST /api/agent/run` (csrf) → `agent.run(...)` result; `SignInRequired` → 401
- [ ] **Step 1: Write the failing tests** with `httpx.MockTransport`: `test_gateway_token_exchanges_as_workbench_agent` (form fields and basic auth), `test_run_reports_each_step`, `test_jira_not_connected_is_a_step_result`, `test_run_endpoint_requires_csrf`.
- [ ] **Step 2: Run** `make test`. Expected: FAIL.
- [ ] **Step 3: Implement.** MCP calls use JSON-RPC over HTTP with `Accept: application/json, text/event-stream`, `MCP-Protocol-Version: 2025-11-25`, stateless (initialize not required by our server; the Jira stub is stateless too).
- [ ] **Step 4: Run** `make test`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Add in-stack sample agent that calls through the gateway"`.

### Task 8: Web UI

**Files:**
- Create: `web/public/connections.html`, `web/public/connections.js`
- Modify: `web/public/index.html`, `web/public/app.js` (link to Connections; "Run agent" button showing step results), `web/public/style.css`

**Interfaces:**
- Consumes: `/api/me` (csrf), `/api/connections*`, `/api/agent/run`.
- Constraints: CSP `script-src 'self'` — no inline scripts or handlers; send `X-CSRF-Token` and rely on same-origin `Origin`.
- [ ] **Step 1: Implement** the page and button (no unit tests; behavior is covered by Task 9 smoke through the same endpoints). Show `connect_url` from an agent step as a link.
- [ ] **Step 2: Verify** in the browser pane at `PUBLIC_URL`: sign in as demo, Connections shows `upstream-demo` not connected; Connect → upstream login → back with `status=success`; Run agent shows three steps with Jira 200. Take one screenshot.
- [ ] **Step 3: Commit** `git commit -m "Add Connections page and Run agent button"`.

### Task 9: Connected-account and in-stack smoke checks

**Files:**
- Modify: `scripts/smoke.py`

**Interfaces:**
- Consumes: Tasks 5–7. Smoke must leave demo unlinked at the end (disconnect) so reruns start clean.
- [ ] **Step 1: Write the smoke checks:**
  7. `PASS: Jira route asks for a connection before linking` (demo via `agent-cli` → 403 `account_not_connected` with `connect_url`)
  8. `PASS: linking through upstream-demo gives the Jira stub the upstream user` (browser login as demo → `POST /api/connections/upstream-demo/connect` → follow redirect → link confirmation page → `demo-upstream` login → `/auth/link-callback` → `status=success`; then `whoami` via gateway returns `demo-upstream`)
  9. `PASS: another user stays unconnected` (demo-other via `agent-cli` on the Jira route → 403 `required_role` `jira:use`; demo-other's `/api/connections` shows `upstream-demo` not connected)
  10. `PASS: in-stack agent runs through the gateway with CSRF` (`/api/agent/run` 403 without CSRF; with CSRF all three steps 200; `docker compose logs gateway` contains `jwt.azp=workbench-agent` and demo's `sub`)
  11. `PASS: disconnect removes the stored upstream token` (`DELETE` → Jira route back to `account_not_connected`)
- [ ] **Step 2: Run** `make smoke`. Expected: fails at the first new check until all prior tasks are in; with them in, 22 PASS lines.
- [ ] **Step 3: Run the whole validation:** `docker compose config --quiet && make up && make smoke && make test && (cd scripts && python3 -m unittest discover -p 'test_*.py')`. Expected: all green, 22 PASS.
- [ ] **Step 4: PUBLIC_URL change check** (Review Focus 4): run `python3 scripts/set_public_url.py http://127.0.0.1:8080`, confirm via kcadm that the IdP `authorizationUrl` and `agent-cli` stay consistent with the new URL, then set it back to `http://localhost:8080` and rerun `make up && make smoke`.
- [ ] **Step 5: Commit** `git commit -m "Smoke-test connected accounts and the in-stack agent"`.

### Task 10: Docs

**Files:**
- Create: `docs/GATEWAY.md`
- Modify: `README.md` (component list and diagram: gateway, credentials, stubs; edges agent→web→gateway, gateway→Keycloak, credentials→Keycloak), `docs/SECURITY.md`, `docs/URLS_AND_CALLBACKS.md` (`/gateway/*`, `/auth/link-callback`, `…/broker/upstream-demo/endpoint`), `docs/CLOUD.md` (gateway is a cloud example, not deployed; https issuer required for GCP)

- [ ] **Step 1: Write `docs/GATEWAY.md`:** the four strategies and which route uses each; the user ∩ agent roles model and how to add an agent client; Connections flow; error table from the spec; adding a real provider (Keycloak built-in GitHub provider with `storeToken`; Atlassian as generic OAuth 2.0/OIDC); GCP recipe from the spec's follow-up section, marked "proven in a spike, not built"; `docker compose restart gateway` resets in-memory budgets; issued gateway tokens stay valid up to 5 minutes after logout.
- [ ] **Step 2: Verify** every command and path in the docs exists (`grep` the referenced files and Make targets).
- [ ] **Step 3: Commit** `git commit -m "Document the outbound gateway"`.
