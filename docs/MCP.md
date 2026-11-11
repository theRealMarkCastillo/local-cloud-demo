# MCP server and Keycloak authorization

The `mcp` service is a sample [Model Context Protocol](https://modelcontextprotocol.io)
server. It lets an MCP client (an agent, IDE or CLI) act on the signed-in user's own
jobs and reports. It is a reference for adding more MCP servers, not a gateway.

## What it exposes

| Tool | Behavior |
| --- | --- |
| `submit_job(title, text)` | Queues a job (title 1–120, text 1–20000 characters); returns `{id}` |
| `list_jobs()` | The caller's 50 newest jobs |
| `get_report(job_id)` | The caller's completed report; anything else is the tool error `Report not found` |

The owner is the same issuer + subject hash the browser app uses, so a user sees the
same jobs in both. The worker is unchanged and still runs jobs at least once.

## How authorization works

The service is an OAuth resource server under the MCP authorization spec:

1. Resource URL: `PUBLIC_URL/mcp`. Without a valid token it answers `401` with
   `WWW-Authenticate: Bearer resource_metadata="PUBLIC_URL/.well-known/oauth-protected-resource/mcp"`.
2. That RFC 9728 metadata names the Keycloak realm issuer
   (`PUBLIC_URL/identity/realms/workbench`) as the authorization server.
3. The client signs the user in with authorization code + PKCE (S256) through the public
   client `workbench-mcp` and requests the scope `mcp:tools`.
4. The `mcp:tools` client scope has an Audience mapper that puts `PUBLIC_URL/mcp` in the
   access token's `aud`. Only `workbench-mcp` offers that scope.
5. On every request the service checks the token's signature (Keycloak JWKS), `iss`,
   `aud`, `exp`, `sub` and the `mcp:tools` scope. Browser-app tokens and tokens for other
   resources are rejected. Tokens are never forwarded upstream or logged.

The service holds no OIDC client secret. It also checks `Host` and `Origin` against
`PUBLIC_URL` (DNS-rebinding protection) and is stateless (no sessions or cookies).

Keycloak setup comes from `scripts/configure_mcp.py`, which `make up`, `make dev` and
`make tunnel-set` run. It is idempotent and preserves users. It is not in the realm
import JSON, because an import that defines client scopes stops Keycloak from creating
its built-in scopes.

## Connect an MCP client locally

| Setting | Value |
| --- | --- |
| Server URL (Streamable HTTP) | `http://localhost:8080/mcp` (or your configured `PUBLIC_URL` + `/mcp`) |
| OAuth client id | `workbench-mcp` (public, no secret) |
| Scope | `mcp:tools` |
| Redirect URI | Any loopback URL, e.g. `http://127.0.0.1:<port>/callback` or `http://localhost:<port>/...` |

The client must support a pre-registered client id. Keycloak advertises a registration
endpoint, but its default policy rejects anonymous Dynamic Client Registration (checked by
`make smoke`), and this demo does not enable it (it is deprecated in MCP 2026-07-28). `make smoke` runs this
whole flow with Python's standard library.

## Optional: clients with no prior registration (CIMD)

MCP clients that only support Client ID Metadata Documents need Keycloak's experimental
features. This is documented, not enabled or tested here:

1. Start Keycloak with `--features=cimd,resource-indicators`.
2. Realm Settings → Client Policies → Profiles: add a profile with the
   `client-id-metadata-document` executor. Set **Trusted domains** to the client vendors
   you accept, keep **Allow http scheme** off outside development, and set
   **Resource indicator allow list** to `PUBLIC_URL/mcp`.
3. Add a client policy with the `client-id-uri` condition (scheme `https`) using that profile.

CIMD requires Keycloak to fetch the client's metadata from a public HTTPS URL, so it only
works through a tunnel or a hosted deployment. See Keycloak's
[MCP integration guide](https://www.keycloak.org/securing-apps/mcp-authz-server).

## Cloud

The cloud manifests do not include `mcp`, and it has not been deployed or runtime-tested.
To add it, run the backend image with `uvicorn app.mcp_server:app` as a private service
next to the API. Give it `ENVIRONMENT`, `PUBLIC_URL`, the database, Redis and S3 settings,
and `OIDC_ISSUER_URL` (plus `OIDC_INTERNAL_URL` only if it differs), and no client
secret. Point the web service's `MCP_UPSTREAM` at it. Configure the hosted IdP with
an equivalent public PKCE client and a scope that puts `PUBLIC_URL/mcp` in `aud`.
