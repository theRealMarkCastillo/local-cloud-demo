# Agent-to-Agent Reporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an A2A route to the outbound gateway and a second in-stack agent, the reporter, so the existing agent calls it on the user's behalf and the reporter in turn calls the workbench MCP server through the gateway, proven by `make smoke`.

**Architecture:** agentgateway gets one route, `a2a-reporter`, with a role check, the `a2a` policy and a token exchange to a new Keycloak client `reporter-agent`. A new `reporter` service (A2A server, existing backend image) verifies its token, exchanges it for a gateway token and calls `list_jobs` through the existing MCP route. The delegation chain is reconstructed from two gateway access-log lines sharing a `traceparent`.

**Tech Stack:** agentgateway v1.7.0 (`a2a` policy), Keycloak 26.8.1 standard token exchange, `a2a-sdk` 1.x, FastAPI/Starlette, httpx, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-10-a2a-reporter-design.md`

## Global Constraints

- Validate with `docker compose config --quiet`, `make up`, `make test`, `make smoke`; config tests with `(cd scripts && python3 -m unittest test_configure_gateway)`.
- No cloud resources. No new host ports; `reporter` is never published.
- `reporter` uses `backend/Dockerfile`. `.env*` stays ignored; the new secret `REPORTER_AGENT_SECRET` has default `local-only-reporter-secret` in `.env.example` and Compose.
- Keep owner-scoped job access, OIDC validation, server-side sessions, CSRF. The existing 79 unit tests, 12 config tests and 23 smoke checks keep passing.
- The four existing gateway routes, `credentials` and the Connections page do not change behaviour.
- Names fixed by the spec: client `reporter-agent`; role `reporter:use` on `gateway`; client scope `agent:reporter`; route name `a2a-reporter` at `/gateway/a2a/reporter`; Compose service `reporter`; skill `job_report`; header `x-calling-agent`; agent step name `reporter`.
- `reporter-agent` scope mapping is exactly `["workbench:read"]`. `reporter:use` is mapped to `workbench-agent` and `agent-cli`, never `agent-cli-writer`.
- No token passthrough, no service account for hop 2, no tokens in logs, A2A messages or step results.
- Failure messages, verbatim: `could not act for this user`, `not allowed to read jobs`.
- No Keycloak delegation feature flags, no `act` claim.
- Commit messages: concise, no AI attribution trailers.

## Review Focus

1. Two users calling the reporter at the same moment must each get only their own report: the reporter keeps no per-user state between requests (no module-level token or MCP session).
2. A slow or unreachable Keycloak or gateway during the reporter's hop 2 must end as a failed task within the HTTP timeout, never a hang or an unhandled 500.
3. A user with zero jobs gets a sensible report (`0 jobs`), not an error or a division/formatting crash.
4. A caller that sends its own `x-calling-agent` header must not be believed: the gateway's value replaces it.
5. Rerunning `scripts/set_public_url.py` with a new URL must keep hop 2 working: `reporter-agent`'s `PUBLIC_URL/gateway` audience mapper and the reporter's issuer both follow `PUBLIC_URL`.

---

### Task 1: Gate — open checks (throwaway probe; only a ledger record is kept)

**Files:** none committed. Work in the session scratchpad with a copy of `gateway/config.yaml` and a Compose override; restore the stack with `make up` afterwards.

- [ ] **Step 1: Agent card behind the prefix.** Run a throwaway A2A hello-world server (from `a2a-sdk` 1.x) as an extra Compose service, add a route `pathPrefix: /gateway/a2a/reporter` with `a2a: {}`, `jwtAuth`, and `urlRewrite.path.prefix: /`. With an `agent-cli` token for `demo`, GET `/gateway/a2a/reporter/.well-known/agent-card.json` through nginx.
  Expected: 200 and the card's interface `url` is `<PUBLIC_URL>/gateway/a2a/reporter/` (or without the trailing slash). **If the URL lacks the prefix or names the backend host and no route setting fixes it, stop and return to design.**
- [ ] **Step 2: Token limits.** Create throwaway client `probe-noread` (public agent client like `agent-cli`, mapped only `reporter:use`), exchange its `demo` token as `gateway` with scope `agent:reporter` (create the scope and `reporter-agent` by hand or with a draft of Task 2), then as `reporter-agent`, and decode the result.
  Expected: record whether `resource_access.gateway.roles` contains `workbench:read`. Ledger: `Ruling: hop-2 limits are user ∩ reporter` (role present) or `user ∩ caller ∩ reporter` (absent). Delete `probe-noread`. No permanent smoke check: every shipped agent client holds `workbench:read`, so the ruling is recorded in the ledger and stated in the docs (Task 6).
- [ ] **Step 3: Headers.** Add to the route a request-header policy setting `x-calling-agent` to `jwt.azp`, and `frontendPolicies.accessLog.add` `traceparent: request.headers["traceparent"]`. Send `SendMessage` with a `traceparent` and a forged `x-calling-agent: evil`.
  Expected: the backend receives `x-calling-agent: agent-cli` and the same `traceparent`; the gateway log line contains the `traceparent`. Ledger the exact config keys that worked. Fallback if the access log cannot carry it: the reporter logs `traceparent`; smoke joins on that.
- [ ] **Step 4: SDK fit.** In a container from `backend/Dockerfile`, `pip install -r requirements.lock 'a2a-sdk>=1,<2'`.
  Expected: installs without changing any already-pinned version; a Starlette app from the SDK serves the card and `SendMessage`, and a token check can run before its handlers. Ledger the import names used and the wire shapes of a completed and a failed `SendMessage` response. Fallback: hand-written JSON-RPC `SendMessage` handler plus a static card in `reporter.py`.
- [ ] **Step 5: Record and clean up.** Append the four results to the ledger, remove the probe files and services, run `make up`.
  Expected: `make smoke` prints 23 PASS lines.

---

### Task 2: Keycloak model

**Files:**
- Modify: `scripts/configure_gateway.py`
- Modify: `scripts/test_configure_gateway.py`
- Modify: `.env.example`, `compose.yaml` (nothing for Keycloak itself; the secret is consumed in Task 3)

**Interfaces:**
- Consumes: `desired(public, secret)`, `confidential(...)`, `agent_mappers(public)`, `audience(name, client_id)`, `secrets()`, `ROLES`.
- Produces: `secrets()["reporter"]` from `REPORTER_AGENT_SECRET` (default `local-only-reporter-secret`); `desired()` additionally returns key `client_scopes`: `{"agent:reporter": {"audience": "reporter-agent", "optional_on": ["gateway"]}}`; `ROLES` includes `reporter:use`; `main()` creates the client scope with an audience mapper and links it as optional on `gateway` (idempotent, same style as `mcp:tools`).

- [ ] **Step 1: Write the failing tests** in `scripts/test_configure_gateway.py` (add `"reporter": "r"` to `SECRETS`):
  - `test_reporter_agent_is_confidential_without_full_scope`: client `reporter-agent` has `publicClient is False`, `fullScopeAllowed is False`, `serviceAccountsEnabled is False`, `standardFlowEnabled is False`, attribute `standard.token.exchange.enabled == "true"`.
  - `test_reporter_agent_maps_only_workbench_read`: `scope_mappings["reporter-agent"] == ["workbench:read"]`.
  - `test_reporter_use_is_mapped_to_callers_only`: holders of `reporter:use` in `scope_mappings` are exactly `["agent-cli", "workbench-agent"]` (sorted).
  - `test_users_group_grants_reporter_use`: `"reporter:use" in groups["gateway-users"]`.
  - `test_reporter_scope_adds_reporter_audience`: `client_scopes["agent:reporter"] == {"audience": "reporter-agent", "optional_on": ["gateway"]}`.
  - Extend `test_only_agent_clients_name_the_gateway_resource` expected holders to `["agent-cli", "agent-cli-writer", "reporter-agent", "workbench-agent"]` (Review Focus 5: built from `public`).
  - Extend `test_agent_scope_mappings_match_spec`: `agent-cli` and `workbench-agent` sets gain `reporter:use`; `agent-cli-writer` unchanged.
- [ ] **Step 2: Run** `(cd scripts && python3 -m unittest test_configure_gateway)`. Expected: the new and extended tests FAIL (KeyError / assertion), the rest pass.
- [ ] **Step 3: Implement** in `configure_gateway.py`: the secret, role, client (`confidential("reporter-agent", secret["reporter"], name="Reporter agent", protocolMappers=agent_mappers(public))`), mappings, group role, `client_scopes`, and the `main()` block that creates/links the scope.
- [ ] **Step 4: Run** the config tests. Expected: all pass (17).
- [ ] **Step 5: Apply and check live.** `python3 scripts/configure_gateway.py` twice.
  Expected: first run lists `client reporter-agent`, `role reporter:use`, the scope; second run prints `nothing (already present)`. `make smoke` still 23 PASS.
- [ ] **Step 6: Commit** `.env.example`, both scripts: `Add reporter-agent client, reporter:use role and agent:reporter scope`.

---

### Task 3: Reporter service

**Files:**
- Create: `backend/app/gateway_client.py`
- Create: `backend/app/reporter.py`
- Create: `backend/tests/test_reporter.py`
- Modify: `backend/app/agent.py` (use `gateway_client`; behaviour unchanged)
- Modify: `backend/app/settings.py` (`REPORTER_CLIENT_ID = "reporter-agent"`)
- Modify: `backend/requirements.txt` (`a2a-sdk>=1,<2`), `backend/requirements.lock` (regenerate in a `python:3.12-slim` container with `pip install -r requirements.lock 'a2a-sdk>=1,<2' && pip freeze`; existing pins must not change)
- Modify: `compose.yaml` (service `reporter`: `<<: *app`, command `uvicorn app.reporter:app …` like `credentials`, env `*app-env` plus `OIDC_ISSUER_URL`, `OIDC_INTERNAL_URL`, `REPORTER_AGENT_SECRET: ${REPORTER_AGENT_SECRET:-local-only-reporter-secret}`, `depends_on` keycloak healthy, `healthcheck: *http-health`, no `ports`), `compose.dev.yaml` (reload override)

**Interfaces:**
- Produces, `gateway_client.py`:
  - `async exchange(http: httpx.AsyncClient, client_id: str, secret: str, subject_token: str) -> httpx.Response` (the token-exchange POST to `s.TOKEN_URL`, basic auth; no status handling).
  - `payload(response: httpx.Response) -> dict` (moved from `agent.py`).
  - `async call_tool(http, token: str, path: str, name: str, *, client_name: str = "workbench-agent", extra_headers: dict[str, str] | None = None) -> tuple[int, dict]` (moved from `agent.py`; `extra_headers` are sent on all three requests).
  - `agent.py` keeps `gateway_token`, `payload`, `call_tool` as names (imported/wrapping) so existing tests pass untouched.
- Produces, `reporter.py`:
  - `AUDIENCE = "reporter-agent"`; `class ReportError(Exception)` with `.message: str`.
  - `summarise(jobs: list[dict]) -> str`: `"0 jobs"` for none; otherwise `"<n> jobs: <count> <status>, …"` with statuses sorted alphabetically, e.g. `3 jobs: 2 complete, 1 queued`. `1 job: 1 queued` for one.
  - `async job_report(token: str, traceparent: str | None, http: httpx.AsyncClient) -> str`: exchange as `reporter-agent` (non-200 → `ReportError("could not act for this user")`); `call_tool(..., "/gateway/mcp/workbench", "list_jobs", client_name="reporter-agent", extra_headers={"traceparent": …} if valid)`; no `structuredContent` or `isError` or non-200 → `ReportError("not allowed to read jobs")`; `httpx.HTTPError` → `ReportError("could not act for this user")`; returns `summarise(jobs)`.
  - `create_app(verifier: mcp_auth.KeycloakTokenVerifier | None = None, http_factory=None)` → ASGI app: `/health` open; every other path requires a bearer token valid for `AUDIENCE` (401 otherwise); A2A card (name `Workbench reporter`, skill id `job_report`) and `SendMessage` per the Task 1 ledger shapes; a `ReportError` becomes a failed task whose status message is `.message`. Log line per request: `sub`, `x-calling-agent`, outcome. `http_factory` defaults to `lambda: httpx.AsyncClient(timeout=15)` and is called per request.
  - `app = create_app()`.
- A `traceparent` is forwarded only if it matches `^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`.

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_reporter.py` (mock transport in the style of `test_agent.Gateway`; a fake verifier returning claims for token strings `demo-token` → sub `demo`, `other-token` → sub `other`):
  - `test_summarise_counts_by_status`: `summarise([{"status":"complete"},{"status":"queued"},{"status":"complete"}]) == "3 jobs: 2 complete, 1 queued"`.
  - `test_summarise_zero_and_one` (Review Focus 3): `summarise([]) == "0 jobs"`; `summarise([{"status":"queued"}]) == "1 job: 1 queued"`.
  - `test_rejects_missing_wrong_audience_and_expired_token`: three requests to the `SendMessage` path each return 401 and no outbound request is made.
  - `test_health_needs_no_token`: 200.
  - `test_job_report_exchanges_then_lists_jobs`: the token endpoint receives basic-auth user `reporter-agent` and `subject_token` equal to the incoming token; the MCP calls carry `Bearer <exchanged>` and never the incoming token; result is `2 jobs: 2 complete`.
  - `test_exchange_refused_is_a_failed_task`: token endpoint 400 → response is a failed task with text `could not act for this user`.
  - `test_denied_tool_is_a_failed_task`: gateway 403 on `tools/call` → `not allowed to read jobs`.
  - `test_unreachable_gateway_is_a_failed_task` (Review Focus 2): transport raises `httpx.ConnectTimeout` → failed task `could not act for this user`, HTTP status of the A2A response is still 200.
  - `test_traceparent_forwarded_only_when_well_formed`: a valid value reaches all MCP requests; `garbage` is dropped and the call still succeeds.
  - `test_concurrent_users_get_their_own_reports` (Review Focus 1): 20 interleaved requests for `demo-token` and `other-token` against a mock that returns 1 job for the exchanged demo token and 3 for the other; every response matches its caller.
  - `test_tokens_never_logged`: capture logs across a success and both failures; neither the incoming nor the exchanged token string appears; `sub` and the `x-calling-agent` value do.
- [ ] **Step 2: Run** `make test`. Expected: `test_reporter` errors on import (`No module named app.reporter`); the other 79 pass.
- [ ] **Step 3: Add the dependency and lock**, rebuild the image (`docker compose build api`). Expected: `git diff backend/requirements.lock` shows only added lines.
- [ ] **Step 4: Implement `gateway_client.py`** and switch `agent.py` to it. Run `make test`. Expected: the 79 existing tests still pass.
- [ ] **Step 5: Implement `reporter.py`.** Run `make test`. Expected: all pass (90).
- [ ] **Step 6: Wire Compose** and run `docker compose config --quiet && make up`. Expected: `reporter` is healthy; `docker compose port reporter 8000` reports no published port.
- [ ] **Step 7: Commit:** `Add reporter A2A agent that reads jobs on the user's behalf`.

---

### Task 4: Gateway route and gateway smoke checks

**Files:**
- Modify: `gateway/config.yaml` (route `a2a-reporter`; access-log `traceparent`), `compose.yaml` (gateway `depends_on` reporter healthy)
- Modify: `scripts/smoke.py`

**Interfaces:**
- Consumes: the Task 1 ledger for the exact `a2a`, header and access-log keys; `reporter` on `reporter:8000`; scope `agent:reporter`.
- Produces: route `a2a-reporter` — `matches: [{path: {pathPrefix: /gateway/a2a/reporter}}]`, `jwtAuth: *jwt`, `authorization.rules: ["'reporter:use' in jwt.resource_access.gateway.roles"]`, `a2a: {}`, `urlRewrite.path.prefix: /`, request header `x-calling-agent` = `jwt.azp`, backend `reporter:8000` with `backendAuth.oauthTokenExchange` identical to the MCP route's except `scopes: ["agent:reporter"]`. Smoke helper `a2a(token, text="report", headers=None) -> (status, body)` posting `SendMessage` to `GATEWAY + "/a2a/reporter"` with `A2A-Version: 1.0`, and `report_text(body) -> str`.

- [ ] **Step 1: Write the smoke checks first** (after the existing gateway section, before the connected-account section):
  - `agent-cli-writer` for `demo`: `a2a(...)` status 403. Print `PASS: A2A route denies an agent without reporter:use`.
  - Card: GET `/a2a/reporter/.well-known/agent-card.json` without a token → 401; with `agent_demo` → 200, a skill with id `job_report`, and the interface URL starts with `GATEWAY + "/a2a/reporter"`. Print `PASS: agent card is served through the gateway with its URL rewritten`.
  - Reports: `report_text(a2a(agent_demo))` starts with `f"{len(demo_ids)} job"` and `a2a(agent_other)` with the count for `demo-other` (use the id sets the gateway section already computes). Print `PASS: reporter reads only the calling user's jobs`.
  - Write refusal: exchange `agent_demo` as `gateway` (`local_env("GATEWAY_CLIENT_SECRET", …)`) with scope `agent:reporter`, then as `reporter-agent` (`local_env("REPORTER_AGENT_SECRET", "local-only-reporter-secret")`); with that token `gw_tool(WB, …, "submit_job", …)` gives the same denial the existing `agent_demo` write check asserts. Print `PASS: the reporter's token cannot write jobs`.
  - Forged header and chain (Review Focus 4): call `a2a(agent_demo, headers={"traceparent": tp, "x-calling-agent": "evil"})` with a fresh `tp`; then in `docker compose logs --since 1m gateway` find one line with `tp`, `a2a-reporter` and `agent-cli`, and one with `tp`, `mcp-workbench` and `reporter-agent`; in `docker compose logs --since 1m reporter` the line for this request names `agent-cli` and never `evil`; neither log contains `agent_demo`. Print `PASS: two audit lines share one traceparent across both hops`.
  - `reporter` has no published port (extend the existing no-ports assertion).
- [ ] **Step 2: Run** `make smoke`. Expected: FAIL at the first new check (404 from the gateway, no such route).
- [ ] **Step 3: Add the route and dependency**, `docker compose config --quiet && make up`.
- [ ] **Step 4: Run** `make smoke`. Expected: 29 PASS lines (23 + 6).
- [ ] **Step 5: Public-URL move (Review Focus 5).** `python3 scripts/set_public_url.py http://127.0.0.1:8080`, `make smoke`, then back to `http://localhost:8080`, `make smoke`. Expected: 29 PASS both times. Ledger the evidence.
- [ ] **Step 6: Commit:** `Route agent-to-agent calls to the reporter through the gateway`.

---

### Task 5: Agent A step, UI and in-stack smoke

**Files:**
- Modify: `backend/app/agent.py`, `backend/tests/test_agent.py`
- Modify: `web/public/app.js` (`STEP_NAMES.reporter = 'Ask the reporter agent (agent to agent)'`), `web/public/index.html` only if the section text lists the steps
- Modify: `scripts/smoke.py`

**Interfaces:**
- Consumes: `gateway_client`, the A2A wire shapes from the Task 1 ledger.
- Produces: `async agent.ask_reporter(http: httpx.AsyncClient, token: str) -> dict` returning `{"step": "reporter", "status": 200, "detail": <report text>}` on a completed task; `{"step": "reporter", "status": 502, "detail": <failure message>}` on a failed task; `{"step": "reporter", "status": <http status>, "detail": "failed"}` on a non-200 response. It sends a fresh `traceparent` (`00-<32 hex>-<16 hex>-01`). `agent.run` appends it as the fourth step. An `httpx.HTTPError` still propagates to the endpoint's existing 502 handling.

- [ ] **Step 1: Write the failing tests** in `test_agent.py` (extend the `Gateway` mock with `/gateway/a2a/reporter`, option `reporter="ok" | "failed" | 403`):
  - `test_run_reports_each_step`: expect four steps; the last is `{"step": "reporter", "status": 200, "detail": "2 jobs: 2 complete"}`.
  - `test_reporter_failed_task_is_a_step_result`: status 502, detail `not allowed to read jobs`; the other three steps unaffected.
  - `test_reporter_forbidden_is_a_step_result`: status 403, detail `failed`.
  - `test_reporter_call_presents_exchanged_token_and_traceparent`: the request carries `Bearer gateway-token` and a well-formed `traceparent`.
- [ ] **Step 2: Run** `make test`. Expected: the four tests FAIL (three steps returned / no such attribute).
- [ ] **Step 3: Implement** `ask_reporter` and the `run` change; update `STEP_NAMES`.
- [ ] **Step 4: Run** `make test`. Expected: all pass (93).
- [ ] **Step 5: Update the in-stack smoke check:** the `/api/agent/run` assertion expects `[("llm", 200), ("list_jobs", 200), ("jira", 200), ("reporter", 200)]`, the reporter detail starts with `f"{…} job"`, and the gateway log has a line with `workbench-agent` and `a2a-reporter` for `demo_sub`. Run `make up && make smoke`. Expected: 29 PASS.
- [ ] **Step 6: Check the UI** in the browser pane: sign in as `demo`, click Run agent. Expected: four rows, the last reading `Ask the reporter agent (agent to agent): 200 <n> jobs: …`.
- [ ] **Step 7: Commit:** `Call the reporter from the in-stack agent and show the step`.

---

### Task 6: Docs

**Files:**
- Modify: `docs/GATEWAY.md` (new section "Agent-to-agent"), `README.md` (diagram edges gateway→reporter→gateway, services table row, smoke line), `docs/SECURITY.md`, `docs/URLS_AND_CALLBACKS.md` (row for `/gateway/a2a/reporter`), `docs/STACK_PATTERN.md`, `docs/CLOUD.md` (one line: local only, not deployed)
- Modify: the spec (append "Changes made during implementation" with every ledger ruling)

- [ ] **Step 1: Write the "Agent-to-agent" section:** the four-step flow; how to join the two audit lines by `traceparent`; the limits verbatim from the spec's "Delegation chain and its limits", with the token-limits sentence set by the Task 1 ruling; the rule that agents never forward a received token.
- [ ] **Step 2: Update the other files.** Every path, client, role and command named must exist: check with `grep` against `gateway/config.yaml`, `scripts/configure_gateway.py`, `compose.yaml`.
- [ ] **Step 3: Run** `grep -c "a2a" README.md docs/GATEWAY.md docs/SECURITY.md docs/URLS_AND_CALLBACKS.md`. Expected: each ≥ 1.
- [ ] **Step 4: Commit:** `Document agent-to-agent calls through the gateway`.
