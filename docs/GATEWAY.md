# Outbound agent gateway

Agents reach LLMs, MCP servers and other services through one gateway at
`PUBLIC_URL/gateway/`. Every call carries the real user, Keycloak decides which
agent may use which service, and the gateway logs the user (`jwt.sub`) and the
agent (`jwt.azp`) for every request. It is validated in local Docker Compose with
stand-ins. It is not deployed or runtime-tested in the cloud.

```mermaid
flowchart LR
  Agent[External agent, PKCE] --> Web[nginx /gateway/]
  API[API: in-stack agent as workbench-agent] --> GW
  Web --> GW[gateway: agentgateway]
  GW -- token exchange, user's identity --> MCP[mcp]
  GW -- asks for the user's stored token --> Cred[credentials]
  Cred -- exchange + broker token read --> KC[Keycloak]
  GW -- user's upstream token --> Jira[stubs: Jira-like MCP]
  GW -- shared system key --> Echo[stubs: echo]
  GW -- provider key + per-user budget --> LLM[stubs: fake LLM]
  GW -- token exchange, user's identity --> Rep[reporter agent, A2A]
  Rep -- its own exchanged token --> GW
```

## Routes and how identity reaches each service

| Route | Role required | Credential the service receives |
| --- | --- | --- |
| `/gateway/mcp/workbench` | `workbench:read` (`submit_job` also needs `workbench:write`) | A Keycloak token for `PUBLIC_URL/mcp` with the user as `sub`, obtained by RFC 8693 token exchange (on-behalf-of). |
| `/gateway/mcp/jira` | `jira:use` | The user's own connected-account token, read from Keycloak by the `credentials` service. |
| `/gateway/echo/*` | `echo:use` | One shared system key held by the gateway. The user's token never reaches the service. |
| `/gateway/llm/v1/*` | `llm:use` | The provider key held by the gateway. Each user has a budget of 1000 tokens per hour, shared across their agents. |
| `/gateway/a2a/reporter` | `reporter:use` | A Keycloak token for `reporter-agent` with the user as `sub`, obtained by token exchange. See [Agent-to-agent](#agent-to-agent). |

The exchanged token names the user and the gateway (`azp=gateway`), not the agent,
and has no `act` claim: Keycloak's standard token exchange does not record a
delegation chain. The agent is enforced before the exchange and recorded in the
gateway's log.

Budgets live in the gateway's memory. `docker compose restart gateway` resets them;
`make smoke` does this itself.

## Governance: user ∩ agent

- The `gateway` client owns the roles `llm:use`, `workbench:read`,
  `workbench:write`, `jira:use` and `echo:use`.
- Users get roles through the groups `gateway-users` and `gateway-jira`.
- Each agent is its own Keycloak client with full scope off and scope mappings for
  only the roles it may use. A token carries only the roles the user holds **and**
  the agent is mapped to.
- The gateway accepts only tokens whose audience includes `PUBLIC_URL/gateway`.
  Only agent clients map that audience. Keycloak adds client-id audiences to any
  token that carries a client's roles, so the client id `gateway` alone must not be
  trusted as the audience.

| Client | Type | May use |
| --- | --- | --- |
| `agent-cli` | Public, PKCE, loopback redirects | `llm:use`, `workbench:read`, `jira:use`, `echo:use` |
| `agent-cli-writer` | Public, PKCE (used by the smoke test) | `workbench:read`, `workbench:write` |
| `workbench-agent` | Confidential; the API's sample agent | `llm:use`, `workbench:read`, `jira:use` |

To add an agent, add a client to `desired()` in `scripts/configure_gateway.py`
with its scope mappings, then run `python3 scripts/configure_gateway.py`. The
script is idempotent and runs on `make up`, `make dev` and `make tunnel-set`.

## Connected accounts

The Connections page (`/connections.html`) links a user's account at an outside
service once. Keycloak stores that service's tokens as a link-only identity
provider (`upstream-demo` locally) and refreshes them when read.

1. Connect redirects through Keycloak's account-linking action
   (`kc_action=idp_link:upstream-demo`) and returns to `/auth/link-callback`.
2. On a Jira-route request the gateway asks `credentials` for the user's token.
   `credentials` exchanges the agent's token for one that carries
   `broker/read-token`, reads the stored token from Keycloak and returns it as the
   upstream `Authorization` header. Only the `gateway-credentials` client is mapped
   to `read-token`, and `configure_gateway.py` turns off full scope on the built-in
   `admin-cli` and `security-admin-console` clients, so agents and browser sessions
   can never read stored tokens.
3. `credentials` caches a token per user and service in process memory for at most
   60 seconds, and never past 30 seconds before it expires.
4. Disconnect unlinks the account in Keycloak and evicts the cached token, so it
   takes effect at once.

Locally the outside service is the `upstream-demo` realm: sign in there as
`demo-upstream` with the demo password.

## Errors

| Condition | Response |
| --- | --- |
| Missing, invalid or wrong-audience token | 401 |
| Role missing for the route | 403, plain text `authorization failed` |
| Tool not allowed for this user and agent | Hidden from `tools/list`; calling it is an MCP error |
| Account not connected | 403 `{"error":"account_not_connected","connect_url":"PUBLIC_URL/connections.html?service=upstream-demo"}` |
| Token budget used up | 429 |
| `credentials` or Keycloak unavailable | 503 |

## Sample in-stack agent

"Run agent" on the home page calls `POST /api/agent/run`. The API exchanges the
user's server-side session token for a gateway token as `workbench-agent`, then
asks the LLM, lists the user's jobs, calls the Jira stand-in and asks the reporter
agent for a report, all through the gateway. The session cookie and the user's own tokens never leave the API.

## Agent-to-agent

One agent can call another through the gateway on the user's behalf. The second
sample agent is the **reporter** (`backend/app/reporter.py`, Compose service
`reporter`, Keycloak client `reporter-agent`). It speaks the A2A protocol
(`a2a-sdk`) and has one skill, `job_report`.

1. The calling agent sends an A2A `SendMessage` to `/gateway/a2a/reporter` with its
   gateway token. The gateway checks `reporter:use`, exchanges the token for one
   addressed to `reporter-agent` (scope `agent:reporter`), and sets
   `x-calling-agent` to the caller's client. A caller cannot forge that header.
2. The reporter checks the token (issuer, audience `reporter-agent`, expiry), then
   exchanges it as `reporter-agent` for a gateway token of its own.
3. The reporter calls `/gateway/mcp/workbench` with that token. The existing route
   exchanges once more for the MCP server, and `list_jobs` returns only the user's jobs.
4. The reporter answers with a completed task such as `3 jobs: 2 complete, 1 queued`.
   When it cannot act it answers with a failed task: `could not act for this user`
   or `not allowed to read jobs`.

The agent card is served at `/gateway/a2a/reporter/.well-known/agent-card.json`. It
needs a gateway token, and the gateway rewrites the URL in it to this route, so
callers never learn the reporter's own address.

**What limits the reporter.** `reporter-agent` is mapped only `workbench:read`, so
the reporter can read a user's jobs and can never write them. Its own mapping is the
cap: the token it exchanges carries the user's roles limited to `reporter-agent`'s
mapping, not also to the calling agent's. The calling agent is checked once, at the
first hop, by `reporter:use`. Give a second agent only the roles it should have
whoever calls it.

**Rules for agents.** Never forward a token you were given; exchange it. Never fall
back to a service account for a user's request.

**Reading the chain from the audit log.** No token names an earlier hop, so the
chain user → agent → reporter → tool is reconstructed from the gateway log. The
gateway keeps the trace id of an incoming `traceparent` header and logs it as
`trace.id`; the reporter forwards the header on its own call. One request leaves two
lines with the same `trace.id` and user (`jwt.sub`):

```bash
docker compose logs gateway | grep 'trace.id=<32 hex digits>'
```

The first has `route=default/a2a-reporter` and `jwt.azp="workbench-agent"` (or
`"agent-cli"`); the second has `route=default/mcp-workbench` and
`jwt.azp="reporter-agent"`. The reporter also logs `sub`, the calling agent and the
outcome.

## Using a real provider instead of the stand-in

Not tested against real accounts.

- **GitHub:** add Keycloak's built-in GitHub identity provider with "Store tokens"
  on and "Account linking only" on, then add its alias to `ALIASES` in
  `backend/app/credentials.py` and `backend/app/connections.py`, and a gateway
  route. GitHub OAuth apps do not issue refresh tokens by default.
- **Atlassian (Jira):** add a generic OAuth 2.0 / OpenID Connect provider with
  Atlassian's endpoints and the scopes the agent needs.
- Scopes are fixed per provider in Keycloak. Asking for more later means changing
  the provider's default scopes and linking again.
- Services that only take per-user API keys need a real secret store. Keycloak is
  not one.

## Proven in a spike, not built: per-user GCP access

1. Give the agent's token (or an exchanged one) the audience
   `https://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`.
2. Exchange it at Google STS (`https://sts.googleapis.com/v1/token`,
   `subject_token_type=urn:ietf:params:oauth:token-type:jwt`).
3. Grant IAM roles to
   `principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/subject/<sub>`.

GCP requires an `https` issuer, so this cannot run against the local `http`
stack. Prefer a public issuer so GCP fetches the signing keys itself; uploaded
keys must be uploaded again whenever Keycloak rotates them. Mapping
`attribute.agent=assertion.azp` would allow per-agent IAM conditions (untested).

## Limits

- Issued gateway tokens stay valid for up to five minutes after logout.
- Stand-in services (`stubs`, the `upstream-demo` realm) are local only.
- No per-agent budgets and no persistent budgets.
- No delegation (`act`) claims: the chain is in the audit log, not in the tokens.
  Keycloak 26.8 has a preview feature, token exchange delegation, that writes an
  `act` claim naming the acting client. It needs feature flags, a consent screen
  per login session and fine-grained admin permissions, and it is one level deep:
  a token that already carries `act` cannot be exchanged again, so it cannot
  express user → agent → reporter. It is not enabled here. Nested `act` chains
  need a token service that nests them.
- The reporter is our own agent in the same realm. Agents from another
  organisation or identity provider are not covered.
- A2A calls block until the reporter answers: no streaming, push notifications or
  long-running tasks. Finished tasks stay in the reporter's memory until it restarts.
- Authorization on the A2A route is per route, not per skill.
