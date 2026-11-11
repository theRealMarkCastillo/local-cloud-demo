# Outbound agent gateway governed by Keycloak

## Goal

Give agents one outbound gateway for LLMs, our own MCP server, shared-key
services and per-user SaaS services (Jira/GitHub style), so that:

- every downstream call carries the real user, never a shared service account,
  except where a service only has one system key;
- which agent may use which service and tool is decided by Keycloak roles;
- every call is audited with the user (`sub`) and the agent (`azp`);
- a user's credentials never reach another user or an agent.

Phase one runs and is smoke-tested in local Docker Compose with stand-ins.
Real Jira/GitHub, GCP and per-user API keys are documented follow-ups.

## Constraints (from AGENTS.md and the existing stack)

- Local Compose validation: `docker compose config --quiet`, `make up`, `make smoke`.
- No paid cloud resources; cloud examples are not described as deployed or tested.
- `.env*` stays ignored; new secrets get placeholder values in `.env.example`.
- Preserve owner-scoped job access, OIDC validation, server-side sessions, CSRF.
- Use the existing backend Dockerfile for our own services; no new public ports;
  no admin or data ports exposed.
- Existing 11 smoke checks keep passing.

## Evidence from the spikes (throwaway, removed)

- Keycloak 26.8 standard token exchange v2: a token with `aud` containing the
  requester, exchanged with `scope=mcp:tools`, yields `sub`=user,
  `aud=PUBLIC_URL/mcp`, `azp`=requester, no `act`, 300 s lifetime; `/mcp`
  accepts it. The `audience` parameter must be a client ID that scopes grant.
- agentgateway v1.7.0 (open source) on one config: `jwtAuth`,
  `backendAuth.oauthTokenExchange` (subject token from
  `jwt.rawToken.unredacted()`, because `jwtAuth` strips the header), route
  `urlRewrite.authority.full` to the public host (else our MCP server returns
  421), `mcpAuthorization` CEL filtering of `tools/list` and `tools/call`,
  `backendAuth.key`, an `ai` backend with `localRateLimit` `type: tokens` keyed
  by `jwt.sub`. Access log carries `jwt.sub`, `mcp.method.name`,
  `gen_ai.tool.name`; `azp` needs an explicit log field.
- Keycloak connected accounts: a link-only OIDC identity provider with
  `storeToken`, linked through `kc_action=idp_link:<alias>` (needs the account
  `manage-account-links` role), returns the stored upstream access and refresh
  token from `GET /realms/workbench/broker/<alias>/token` only to a token whose
  client and user both carry `broker/read-token`; Keycloak refreshes the stored
  token on read after expiry.
- GCP: a Keycloak token with the provider resource name as audience, through
  GCP STS, gives a federated token; IAM grants per `principal://…/subject/<sub>`
  allow one user and deny another. GCP requires an `https` issuer.
- agentgateway's schema has HTTP ext-authz with `includeResponseHeaders`
  (copy authz-service response headers into the backend request). Not yet run.

## Architecture

```
agent (external PKCE client)  ─┐
in-stack agent (api, as        ├─► web (nginx) /gateway/* ─► gateway (agentgateway)
  workbench-agent)            ─┘                               │
                                   ┌───────────────────────────┼───────────────┬───────────────┐
                                   ▼                           ▼               ▼               ▼
                       /gateway/mcp/workbench       /gateway/mcp/jira   /gateway/echo   /gateway/llm/v1/*
                       OBO exchange → mcp:8000      ext-authz →         shared key →    provider key +
                                                    credentials →       stubs echo      per-user budget →
                                                    stubs jira MCP                      stubs fake LLM
```

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `gateway` service | `ghcr.io/agentgateway/agentgateway:v1.7.0`, config `gateway/config.yaml` | JWT check, role checks, tool filtering, credential strategy per route, audit log. Internal only; admin port never published. |
| `credentials` service | backend image, `app.credentials:app` | ext-authz endpoint that returns the user's stored SaaS token as an `Authorization` header. Internal only. |
| `stubs` service | backend image, `app.stubs:app` | Local stand-ins: fake OpenAI-compatible LLM (fixed 500-token usage), echo of request headers, fake "Jira" MCP server that only accepts tokens issued by the `upstream-demo` realm. Not in cloud definitions. |
| `identity/upstream-demo-realm.json` | Keycloak import | Stand-in SaaS authorization server: client `workbench-broker`, user `demo-upstream`, scopes `jira.read`. Local only. |
| `scripts/configure_gateway.py` | idempotent kcadm script | Creates gateway clients, roles, group, scope mappings, mappers and the link-only identity provider (same pattern as `configure_mcp.py`; run by `make up`/`make dev` and `set_public_url.py`). |
| `backend/app/session.py`, `main.py` | existing | Store the user's access/refresh tokens server-side; new `/api/connections*`, `/auth/link-callback`, `/api/agent/run`. |
| `backend/app/agent.py` | new | Scripted sample agent used by `/api/agent/run`. |
| `web/public` | existing | Connections page and a "Run agent" button. |

## Keycloak model

### Clients

| Client | Type | Purpose |
|---|---|---|
| `gateway` | confidential, standard token exchange enabled, no browser flows | Audience for agent tokens; owner of service roles; performs OBO exchange to `/mcp` (optional scope `mcp:tools`). |
| `gateway-credentials` | confidential, standard token exchange enabled | Used by the `credentials` service; the only client scope-mapped to `broker/read-token`. |
| `workbench-agent` | confidential, standard token exchange enabled | Identity of the in-stack agent. |
| `agent-cli` | public, PKCE S256, loopback redirects | Sample external agent. |
| `agent-cli-writer` | public, PKCE S256, loopback redirects | Smoke-only second agent allowed `workbench:write`. |
| `workbench` | existing confidential app client | Gains an audience mapper adding `workbench-agent`, so the API can exchange the user's session token as `workbench-agent`. |

### Roles and governance (user ∩ agent)

- `gateway` client roles: `llm:use`, `workbench:read`, `workbench:write`,
  `jira:use`, `echo:use`.
- Group `gateway-users` holds `llm:use`, `workbench:read`, `workbench:write`,
  `echo:use` and `broker/read-token`; group `gateway-jira` holds `jira:use`.
  `demo` is in both; `demo-other` only in `gateway-users` (smoke: a user
  without the role).
- Every agent client has `fullScopeAllowed=false` and is scope-mapped only to
  the gateway roles it may use. The token therefore carries only roles both
  the user holds and the agent is allowed:
  - `agent-cli`: `llm:use`, `workbench:read`, `jira:use`, `echo:use`
  - `agent-cli-writer`: `workbench:read`, `workbench:write`
  - `workbench-agent`: `llm:use`, `workbench:read`, `jira:use`
- Every agent client has audience mappers adding `gateway` and
  `gateway-credentials`. Keycloak only lets a client exchange a token whose
  audience contains it, so `credentials` needs the second audience. Only the
  internal `credentials` service accepts that audience.
- Users get the account role `manage-account-links` (needed to link accounts).
- Only `gateway-credentials` is scope-mapped to `broker/read-token`; users get
  `broker/read-token` through `gateway-users`. Agent clients have no full
  scope and no mapping to it, so agent tokens never carry it. The existing
  `workbench` client gets `fullScopeAllowed=false` with explicit mappings for
  what it uses today, so session tokens do not carry it either.

### Connected-account provider

Identity provider alias `upstream-demo`: OIDC, `linkOnly=true`,
`storeToken=true`, PKCE S256, `defaultScope="openid jira.read"`, endpoints of
the `upstream-demo` realm. Browser-facing URLs use `PUBLIC_URL`; Keycloak's
back-channel calls reach itself on `http://localhost:8080/identity`.

## Request flows

1. **External agent sign-in.** `agent-cli` logs in with PKCE for the user;
   token has `aud` ∋ `gateway`, `azp=agent-cli`, roles = user ∩ agent.
2. **In-stack agent sign-in.** `POST /api/agent/run` (session cookie + CSRF).
   The API refreshes the stored access token if it expires within 30 s, then
   exchanges it as `workbench-agent` for a token with `aud` ∋ `gateway`, and
   calls the gateway exactly like an external agent.
3. **Every gateway request.** `jwtAuth` (issuer, `aud=gateway`, expiry,
   signature via internal JWKS URL); route role check by CEL on
   `jwt.resource_access.gateway.roles`; access log adds `jwt.sub`, `jwt.azp`,
   route, MCP method, tool, status.
4. **`/gateway/mcp/workbench` (OBO).** Requires `workbench:read`. The gateway,
   as `gateway`, exchanges with `scope=mcp:tools`; rewrites authority to the
   public host; calls `mcp:8000`. `mcpAuthorization`: `list_jobs`,
   `get_report` need `workbench:read`; `submit_job` needs `workbench:write`.
5. **`/gateway/mcp/jira` (connected account).** Requires `jira:use`. HTTP
   ext-authz to `credentials`, forwarding `Authorization`. `credentials`:
   verifies the JWT itself (`KeycloakTokenVerifier`, audience `gateway`,
   requires `jira:use`); in-memory cache keyed by (`sub`, alias), entry TTL
   min(upstream expiry − 30 s, 60 s); on miss, exchanges the user token as
   `gateway-credentials` for a token to itself carrying `read-token`, calls the
   broker token endpoint, returns `Authorization: Bearer <upstream token>`,
   which the gateway copies to the backend request.
6. **`/gateway/echo` (shared key).** Requires `echo:use`; `backendAuth.key`
   from `GATEWAY_ECHO_KEY`.
7. **`/gateway/llm/v1/*`.** Requires `llm:use`; `ai` backend (OpenAI
   provider, host override to `stubs`), provider key from `GATEWAY_LLM_KEY`;
   `localRateLimit` tokens 1000/h keyed by `jwt.sub`. Budget state is in
   gateway memory and resets on restart.
8. **Connections.** `GET /api/connections` lists linked accounts through
   Keycloak's account API with the user's session token. `POST
   /api/connections/upstream-demo/connect` returns a redirect URL with
   `kc_action=idp_link:upstream-demo`; `/auth/link-callback` checks state and
   `kc_action_status`. `DELETE /api/connections/upstream-demo` unlinks through
   the account API, which drops the stored token. Logout deletes session
   tokens; links persist; issued gateway tokens stay valid up to 5 minutes.

## Errors

| Condition | Response |
|---|---|
| Missing or invalid token | 401 with `WWW-Authenticate` |
| Route role missing | 403 `{"error":"forbidden","required_role":"<role>"}` |
| Tool not allowed | MCP error; tool hidden from `tools/list` |
| Account not linked, or stored refresh fails | 403 `{"error":"account_not_connected","connect_url":"PUBLIC_URL/connections?service=upstream-demo"}` |
| Budget exhausted | 429 |
| `credentials` or Keycloak unavailable | 503 (ext-authz failure mode deny) |
| Upstream failure | 502 |
| In-stack agent refresh token expired | 401 to the browser; the UI sends the user to log in |

## Security rules

- Upstream tokens are never logged, never stored outside Keycloak and the
  `credentials` process memory, never returned to agents or browsers.
- The user's own Keycloak access and refresh tokens live only in the existing
  server-side session store, are never sent to the browser, and are deleted at
  logout.
- Client secrets for `gateway`, `gateway-credentials`, `workbench-agent`,
  `workbench-broker`, and keys `GATEWAY_ECHO_KEY`, `GATEWAY_LLM_KEY` come from
  `.env` (placeholders in `.env.example`).
- Gateway and `credentials` publish no host ports; only nginx `/gateway/` is
  public. The gateway admin/UI port stays unpublished.
- The session cookie never reaches the gateway; the API sends only exchanged
  tokens.
- Existing owner scoping, OIDC validation, server-side sessions and CSRF stay.

## Open checks (first plan task; fallback named)

1. agentgateway HTTP ext-authz copies `Authorization` from the `credentials`
   response into the backend request, and passes the deny status and body
   through. Fallback: `credentials` proxies the connected-account routes itself.
2. Exchanging an agent token as `gateway-credentials` yields a token with
   `broker/read-token` that the broker endpoint accepts (the spike only read
   with a directly issued token), and exchanging the session token as
   `workbench-agent` yields `aud` ∋ `gateway`, `gateway-credentials` from that
   client's audience mappers. No fallback: if either fails, stop and re-design
   that path before building on it.
3. agentgateway serves the `ai` backend under a path prefix on the same
   listener (`/gateway/llm/v1/*`). Fallback: a second internal listener,
   still routed only through nginx.

## Testing

- Unit (backend image): `credentials` JWT checks, cache TTL and isolation by
  user and service, broker-response mapping, Keycloak-down 503; API session
  token storage and refresh, CSRF on new endpoints, link-callback handling;
  `configure_gateway.py` idempotency.
- Smoke additions: no token 401; `/mcp`-audience token 401; `agent-cli` sees
  only read tools and is blocked from `submit_job`; `agent-cli-writer` can
  `submit_job`; `demo-other` 403 on Jira through `agent-cli`; OBO `list_jobs`
  owner-scoped for both users; Jira before linking → `account_not_connected`
  with `connect_url`; after linking through `upstream-demo` the Jira stub sees
  `demo-upstream`'s token; `demo-other` still not connected; echo sees only the
  shared key; LLM budget 429 for demo only; `/api/agent/run` 403 without CSRF,
  completes with CSRF, audit shows `azp=workbench-agent` and demo's `sub`;
  gateway admin port and `credentials` unreachable from the host.

## Docs

- New `docs/GATEWAY.md`: strategies, roles model, adding a real provider
  (Keycloak's built-in GitHub provider; Atlassian as generic OAuth/OIDC), and
  the GCP recipe below.
- Update README diagram, SECURITY.md, URLS_AND_CALLBACKS.md, CLOUD.md (cloud
  examples stay "not deployed").

## Documented follow-up: GCP per-user access

Proven in a spike, not built: Keycloak token exchange to a token whose audience
is `https://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`,
then GCP STS token exchange (`subject_token_type=…:jwt`); IAM grants on
`principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/subject/<sub>`.
Needs an `https` issuer (deployed stack). Prefer a public issuer so GCP fetches
JWKS; uploaded JWKS must be re-uploaded on Keycloak key rotation.
`attribute.agent=assertion.azp` enables per-agent IAM conditions (untested).

## Out of scope

Per-user API keys and a secret store; real Jira/GitHub/Entra accounts;
Keycloak delegation (`act`) and Okta Cross App Access; per-agent budgets;
persistent budgets; GCP build-out; cloud deployment.
