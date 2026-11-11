# Agent-to-agent calls through the gateway

## Goal

Let one agent call another through the outbound gateway on a user's behalf, so that
agent-to-agent (A2A) traffic gets the same governance as tool and LLM traffic:

- the called agent sees the real user, never a service account;
- which agent may call which agent is decided by Keycloak roles;
- the called agent can do at most what its own Keycloak mapping allows for that user;
- every hop is audited, and the hops of one request can be joined into one chain.

The demonstration is a two-hop chain: the existing in-stack agent ("agent A",
`workbench-agent`) calls a new in-stack agent, the **reporter**, which itself calls the
workbench MCP server through the gateway.

Agreed in design (2026-11-10): two hops; the reporter is our own agent in the same
Keycloak realm; a token exchange at every hop; the delegation chain lives in the audit
log, not in the tokens.

## Constraints (from AGENTS.md and the existing stack)

- Local Docker Compose only. No cloud resources. No new host ports.
- Use the existing backend Dockerfile for the new service.
- Preserve owner-scoped job access, OIDC validation, server-side sessions and CSRF.
- Keep `.env*` secrets ignored; new secrets get `local-only-` defaults like the others.
- Validate with `docker compose config --quiet`, `make up`, `make test`, `make smoke`.
- The four existing gateway routes, the `credentials` service and the Connections page
  do not change.

## Architecture

```
browser ── Run agent ──▶ api (agent A, workbench-agent)
                          │ 1. exchange session token → gateway token
                          ▼
                       gateway  /gateway/a2a/reporter      (role reporter:use, a2a policy)
                          │ 2. exchange → token for reporter-agent
                          ▼
                       reporter (A2A server, skill job_report)
                          │ 3. exchange its token → gateway token (as reporter-agent)
                          ▼
                       gateway  /gateway/mcp/workbench     (existing route, unchanged)
                          │ 4. exchange → token for the MCP server
                          ▼
                       mcp  list_jobs  (owner-scoped to the user)
```

### Units

| Unit | Responsibility | Depends on |
|---|---|---|
| `backend/app/reporter.py` | A2A server with one skill, `job_report`. Verifies its token, exchanges it, calls `list_jobs` through the gateway, returns a short report. | `a2a-sdk`, `mcp_auth.KeycloakTokenVerifier`, Keycloak token endpoint, gateway |
| `gateway/config.yaml` route `a2a-reporter` | Role check, A2A policy, token exchange to the reporter, calling-agent header. | Keycloak, reporter |
| `scripts/configure_gateway.py` | Client `reporter-agent`, role `reporter:use`, client scope `agent:reporter`, mappings. | Keycloak admin |
| `backend/app/agent.py` | New step `reporter`: one A2A `SendMessage` through the gateway. | gateway |
| `web/public` | The Run agent panel shows the fourth step. | API |

## Keycloak model

### Client `reporter-agent`

- Confidential, standard token exchange enabled, `fullScopeAllowed: false`, no standard
  flow, no direct grants, no service account.
- Secret `REPORTER_AGENT_SECRET` (default `local-only-reporter-secret`), in
  `.env.example` and Compose.
- Scope mapping: `workbench:read` only. The reporter can read a user's jobs and can
  never write them, even when the user and the calling agent both may.
- Protocol mappers: the same three as the other agent clients (`gateway`,
  `gateway-credentials` client audiences and the `PUBLIC_URL/gateway` resource
  audience), so the token it exchanges for hop 2 is accepted at the gateway.

### Role `reporter:use`

- A new client role on `gateway`.
- Granted to users through the `gateway-users` group.
- Mapped to agent clients `workbench-agent` and `agent-cli`. Not mapped to
  `agent-cli-writer`, which is the denied caller in the smoke test.

### Client scope `agent:reporter`

- An optional client scope on the `gateway` client with an audience mapper for
  `reporter-agent`. The gateway requests it when exchanging for the reporter, the same
  pattern as `mcp:tools` for the MCP server.

## Gateway route `a2a-reporter`

- Match: path prefix `/gateway/a2a/reporter`, rewritten to `/`.
- Policies: the shared `jwtAuth`; `authorization` rule
  `'reporter:use' in jwt.resource_access.gateway.roles`; `a2a: {}`;
  `backendAuth.oauthTokenExchange` as client `gateway` with scope `agent:reporter`;
  an added request header `x-calling-agent` set to `jwt.azp`.
- Backend: `reporter:8000`.
- The agent card is served at `/gateway/a2a/reporter/.well-known/agent-card.json` and
  requires a gateway token like every other gateway path. The gateway rewrites the URL
  in the card to its own public URL.
- The access log adds the `traceparent` header value, so the two hops of one request
  can be joined.

## The reporter service

- Compose service `reporter`: existing backend image, `uvicorn app.reporter:app`, no
  published ports, healthcheck on `/health`, reload override in `compose.dev.yaml`.
- Built on `a2a-sdk` 1.x (pinned in the backend requirements). It serves the agent card
  and the JSON-RPC `SendMessage` method. No streaming, no push notifications, no
  long-running tasks: `SendMessage` blocks until the report is ready.
- Agent card: name "Workbench reporter", one skill `job_report` ("Summarise the
  signed-in user's jobs"), text in and text out.
- Token check on every request except `/health`: issuer is the workbench realm, audience
  is `reporter-agent`, not expired. The user is `sub`.
- Skill `job_report`:
  1. Exchange the incoming token at Keycloak as `reporter-agent` (basic auth) for a
     token with the gateway audience.
  2. Open an MCP session on `GATEWAY_INTERNAL_URL/gateway/mcp/workbench` and call
     `list_jobs`, forwarding the incoming `traceparent`.
  3. Reply with a completed task whose text is a one-line report, for example
     `3 jobs for demo: 2 finished, 1 queued`.
- Logging: `sub`, the `x-calling-agent` value and the outcome. Never a token.

## Agent A

- `agent.run` gains a fourth step, `reporter`, after `jira`: one `SendMessage` to
  `GATEWAY_INTERNAL_URL/gateway/a2a/reporter` with a fresh `traceparent`, using plain
  `httpx` like the MCP steps.
- Step result shape is unchanged: `{step, status, detail}`. `detail` is the report text
  on success or the failure message.

## Errors

| Situation | Result |
|---|---|
| Caller's token lacks `reporter:use` (user or agent) | Gateway 403; the reporter is not reached. |
| Missing, expired or wrong-audience token at the reporter | Reporter 401. Only reachable by bypassing the gateway. |
| Keycloak refuses the reporter's exchange | Failed A2A task, message "could not act for this user"; status code logged. |
| Hop 2 denied (for example the user lacks `workbench:read`) | Failed A2A task, message "not allowed to read jobs". |
| Reporter or gateway unreachable | Agent A's step shows 502, as the other steps do. |
| A token exchanged as `reporter-agent` calls a write tool | Denied at the gateway: `reporter-agent` has no `workbench:write`. |

A failed task is a normal step result for agent A, not an exception.

## Delegation chain and its limits

- Each exchanged token keeps the user as `sub` and names the exchanging client as
  `azp`. No token names an earlier hop.
- The chain is reconstructed from the gateway access log: two lines with the same
  `traceparent` and `sub` — route `a2a-reporter` with `azp` `workbench-agent`, then
  route `mcp-workbench` with `azp` `reporter-agent`. The reporter also logs
  `x-calling-agent`, which it can trust because only the gateway can reach it.
- Keycloak 26.8 has a preview feature, token exchange delegation, that writes an `act`
  claim naming the acting client. It needs feature flags, a per-session consent screen
  and fine-grained admin permissions, and it is one level deep: a token that already
  carries `act` cannot be exchanged again, so it cannot express user → A → reporter.
  This design does not enable it. Nested `act` chains need a token service that nests
  them and are out of scope.
- Which limits reach the final MCP token (user ∩ reporter, or user ∩ agent A ∩
  reporter) is an open check below. The docs state whichever is true.

## Security rules

- No token passthrough: the reporter never forwards the token it received.
- The reporter never uses a service account for hop 2.
- No tokens in logs, in A2A messages or in step results.
- `reporter` publishes no host port and is reachable only on the Compose network.
- `reporter-agent` is added to the config test that asserts agent clients have no full
  scope.

## Open checks (first plan task)

1. **Agent card behind a prefix.** With `a2a: {}` and the prefix rewrite, the card URL
   must be the gateway's public URL including `/gateway/a2a/reporter`. If it is not,
   stop and return to design: the route cannot work as specified.
2. **Token limits.** Using a throwaway caller client without `workbench:read`, find
   whether the reporter's hop-2 token still reaches `list_jobs`. The result fixes one
   docs sentence; it does not change the design. No shipped client lacks
   `workbench:read`, so this is gate evidence, not a permanent smoke check.
3. **Headers.** `traceparent` and `x-calling-agent` reach the reporter and both appear
   in the gateway access log. Fallback: log `traceparent` in the reporter and join on
   `sub` plus time.
4. **SDK fit.** `a2a-sdk` 1.x installs in the existing backend image and accepts a
   token check in front of its handlers. Fallback: a minimal hand-written JSON-RPC
   `SendMessage` handler and a static agent card.

## Testing

**Unit** (`backend/tests/test_reporter.py`, additions to `test_agent.py` and
`scripts/test_configure_gateway.py`)

- The reporter returns 401 for a missing token, a wrong audience and an expired token.
- With a fake gateway and token endpoint, `job_report` exchanges the incoming token,
  calls `list_jobs` and returns a completed task with the right counts.
- A refused exchange and a denied MCP call each return a failed task with the stated
  message; no token appears in captured logs.
- The outgoing MCP call carries the incoming `traceparent`.
- Agent A's `reporter` step reports success, a failed task and an unreachable gateway
  in the `{step, status, detail}` shape.
- Desired Keycloak state: `reporter-agent` has no full scope and maps only
  `workbench:read`; `agent-cli-writer` has no `reporter:use`; `agent:reporter` is
  optional on `gateway`.

**Smoke** (`scripts/smoke.py`, live stack)

- `agent-cli-writer` gets 403 on the A2A route.
- The agent card needs a token and shows the rewritten URL.
- `demo` gets a report covering only `demo`'s jobs; `demo-other` only their own.
- A token exchanged as `reporter-agent` is refused `submit_job`.
- Run agent returns four successful steps; the gateway log has the two lines sharing
  one `traceparent`.
- No token in the gateway or reporter logs; `reporter` publishes no host port.

## Docs

- `docs/GATEWAY.md`: an "Agent-to-agent" section with the flow, how to join the audit
  lines, and the limits above.
- `README.md` (diagram, services table), `docs/SECURITY.md`,
  `docs/URLS_AND_CALLBACKS.md`, `docs/STACK_PATTERN.md`: matching rows.

## Out of scope

- Agents from another organisation or identity provider.
- `act` claims, Keycloak delegation, or a token service of our own.
- Agent discovery or a registry beyond one agent card per route.
- Streaming, push notifications and long-running A2A tasks.
- Per-skill authorization; the role check is per route.

## Changes made during implementation

Decisions taken while building, with the evidence in the plan ledger:

- **Token limits (open check 2).** The reporter's hop-2 token carries the user's roles
  limited to `reporter-agent`'s mapping only. The calling agent's limits do not reach
  hop 2: the hop-1 token is issued to the `gateway` client and carries all of the user's
  gateway roles. The calling agent is checked once, by `reporter:use`.
- **Audit join.** The gateway rewrites the span id of `traceparent` and logs the trace id
  natively as `trace.id`. The two hops are joined on `trace.id`, and no custom access-log
  field was added.
- **Calling-agent header.** Set with `transformations.request.set` on the route; it
  replaces a client-supplied value.
- **SDK.** `a2a-sdk` 1.3.0 is used (no fallback). It adds 16 packages to the backend
  image, including `protobuf` and `google-api-core`. Finished tasks stay in the SDK's
  in-memory task store until the reporter restarts, scoped per user.
- **Shared client code.** The token exchange and MCP tool call moved from `agent.py`
  into `backend/app/gateway_client.py`; `mcp_auth.from_settings` takes the audience and
  scope as parameters.
- **Reporter logging.** The reporter installs its own INFO log handler; uvicorn configures
  only its own loggers.
- **Smoke.** Five new PASS lines (28 in total); the no-ports check extends an existing
  line. The token-limits check is gate evidence only.
- **Agent step.** A 200 response without a task is shown as a failed step (502).
