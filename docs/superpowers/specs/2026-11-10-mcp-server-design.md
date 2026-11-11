# Sample MCP server secured by Keycloak

Date: 2026-11-10. Status: design approved; awaiting spec review.

## Goal

Add one sample MCP server to the local stack that lets an MCP client (an agent,
IDE or CLI) act on the signed-in user's own jobs and reports, authorized by the
existing Keycloak realm under the MCP authorization spec. It is a reference for
adding further MCP servers later.

Success means: an MCP client discovers the authorization server from the MCP
server, signs in as `demo` with OAuth 2.1 + PKCE, submits a job, sees it finish,
and downloads the report; `demo-other` cannot see it; tokens not issued for the
MCP server are rejected. `make smoke` proves all of this locally.

## Constraints (from AGENTS.md and the existing stack)

- Use the existing backend Dockerfile; no new image.
- Preserve owner-scoped job access (issuer + subject hash), OIDC validation,
  server-side sessions and CSRF checks for the browser app.
- Worker remains at-least-once and unchanged.
- No new host ports; only nginx on loopback is published.
- No cloud deployment; cloud notes are documentation only.

## Architecture

New Compose service `mcp`, built from `backend/Dockerfile`, command
`uvicorn app.mcp_server:app` on port 8000. It depends on `init`, `keycloak`
and `redis` like `api`. nginx routes to it:

- `/mcp` (Streamable HTTP transport endpoint)
- `/.well-known/oauth-protected-resource/mcp` (RFC 9728 metadata)

nginx gets a new `MCP_UPSTREAM` variable. The browser app's routes are unchanged.

The resource identifier is `${PUBLIC_URL}/mcp`.

### Units

| Unit | Purpose | Depends on |
| --- | --- | --- |
| `app/jobs.py` | `create_job(owner, title, text)`, `list_jobs(owner)`, `read_report(owner, job_id)`; owner-scoped SQL, Redis wake-up, MinIO read. Moved from `main.py`. | `services`, `settings` |
| `app/identity.py` | `owner_id(issuer, sub)`: the shared issuer + subject hash. | none |
| `app/mcp_auth.py` | `KeycloakTokenVerifier`: validates a bearer JWT and returns the SDK's `AccessToken` (subject, scopes, expiry, resource) or `None`. | PyJWT (installed with the `mcp` SDK), JWKS found via OIDC discovery on the internal issuer URL |
| `app/mcp_server.py` | `MCPServer` (MCP Python SDK 2.x; formerly FastMCP) with three tools; the SDK serves protected-resource metadata and enforces Host/Origin. Stateless HTTP with JSON responses. | `jobs`, `mcp_auth`, `identity`, `mcp` SDK |
| `app/main.py` | Browser API, now calling `jobs` and `identity`. Behavior unchanged. | as today |

### Tools

- `submit_job(title: str, text: str) -> {id}`: same limits as the web form
  (title 1-120, text 1-20000 characters).
- `list_jobs() -> {jobs: [...]}`: newest 50 owned by the caller.
- `get_report(job_id: str) -> report JSON`: only for the caller's completed job;
  otherwise a tool error "Report not found".

The owner is always derived from the validated token: `owner_id(OIDC_ISSUER_URL, sub)`.
That matches the web app, so the same user sees the same jobs in both.

## Authorization

### Token validation (`KeycloakTokenVerifier`)

Each request with a token is validated against Keycloak's signing keys (fetched
from the internal issuer URL and cached; refetched once on an unknown `kid`):

- signature with an allowed asymmetric algorithm (RS256/ES256 family)
- `iss` equals `OIDC_ISSUER_URL`
- `aud` contains `MCP_RESOURCE_URL` (`${PUBLIC_URL}/mcp`)
- `exp` in the future (small clock-skew allowance), `sub` present
- `scope` contains `mcp:tools`

Failure means the SDK returns `401` with `WWW-Authenticate: Bearer
resource_metadata=".../.well-known/oauth-protected-resource/mcp"` (missing or invalid
token) or `403 insufficient_scope`. Token values are never logged. The MCP service gets no OIDC client secret.

### Keycloak realm changes (`identity/workbench-realm.json`)

- Optional client scope `mcp:tools` with an Audience mapper whose included custom
  audience is `${PUBLIC_URL}/mcp` (access token only).
- Public client `workbench-mcp`: standard flow only, PKCE S256 required, no direct
  access grants, `mcp:tools` as an optional scope, redirect URIs limited to
  loopback (`http://127.0.0.1/*`, `http://localhost/*`) for local MCP clients.
  Implementation must confirm Keycloak accepts arbitrary loopback ports for these
  (RFC 8252); if not, list explicit ports used by the smoke test and document it.
- The existing `workbench` client does not get `mcp:tools`, so web tokens lack
  the MCP audience and are rejected.
- No anonymous Dynamic Client Registration (deprecated in MCP 2026-07-28).

Existing Keycloak volumes keep the old realm, so `scripts/configure_mcp.py` applies the
scope and client idempotently to a running realm (users preserved). `make up`/`make dev`
run it after `configure_email.py`, and `set_public_url.py` reruns it because the
audience contains `PUBLIC_URL`.

### Optional, documented only: CIMD and resource indicators

For tunnel or hosted use with clients that have no prior registration, docs
describe enabling `--features=cimd,resource-indicators` (experimental in Keycloak)
plus the `client-id-metadata-document` client policy with a trusted-domain list
and a resource allow-list of `${PUBLIC_URL}/mcp`. Not enabled by default because
it is experimental and needs public HTTPS client metadata URLs.

## Transport security

- Origin header validated against `PUBLIC_URL` when present (DNS-rebinding
  protection required by the MCP transport spec).
- Request body size limited at nginx as today (32k is enough for 20k text).
- No sessions or cookies on the MCP service; stateless HTTP mode.

## Testing

- Unit tests (`backend/tests/test_mcp_auth.py`) with a locally generated RSA key:
  valid token accepted; wrong audience, wrong issuer, expired, missing scope,
  bad signature, `alg: none` all rejected.
- `scripts/smoke.py` MCP section (stdlib only):
  1. `POST /mcp` without a token returns 401 with `resource_metadata`.
  2. Fetch protected-resource metadata; it names the realm issuer.
  3. Authorization code + PKCE login as `demo` through `workbench-mcp` with a
     loopback redirect URI (the redirect is captured, not served).
  4. `initialize`, `tools/list` (three tools), `submit_job`, poll `list_jobs`
     until complete, `get_report`.
  5. Log in as `demo-other`: `list_jobs` lacks the job; `get_report` fails.
- Validation: `docker compose config --quiet`, `make up`, `make smoke`, unit tests.

## Docs

README (service list, `make smoke` coverage, MCP client connection steps),
`STACK_PATTERN.md` (MCP row), `SECURITY.md` (token audience, no secret in MCP
service), `CLOUD.md` (how to add the `mcp` service; CIMD opt-in; not deployed),
`URLS_AND_CALLBACKS.md` (resource URL and loopback redirects).

## Out of scope

Fly/Render manifests for `mcp`; MCP prompts/resources; DCR; consent screens
beyond Keycloak defaults; refresh-token handling in the smoke test.
