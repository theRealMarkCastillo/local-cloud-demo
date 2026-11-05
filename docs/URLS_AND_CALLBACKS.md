# Public URLs and callbacks across environments

This is one configuration pattern for local Compose, shared local demos and cloud.
The tunnel is optional local transport; it does not define application architecture.
Every environment has a configured public origin and separate internal service URLs.

| Environment | Public origin example | Internal network |
| --- | --- | --- |
| Local Compose | `http://localhost:8080` | Compose service names |
| Shared local demo | `https://your-project.ngrok.app` | The same Compose service names |
| Cloud | `https://dev.example.com` | Provider private networking or managed-service endpoints |

## Configuration contract

- `PUBLIC_URL` is the absolute browser-facing origin: scheme and hostname, optional
  port, no credentials or path. This scaffold expects hosting at the origin root;
  arbitrary subpath hosting needs explicit framework/proxy changes.
- Derive app links and local Auth callback addresses from this origin. Prefer relative
  browser API paths. Do not infer security-sensitive redirect destinations from an
  arbitrary incoming Host or forwarded header.
- Browser endpoints must be reachable by the tester's device. `localhost` in a
  remote browser means that remote device. Never serialize Compose hostnames or
  database connection strings into frontend config.
- Keep internal API/Auth/database/cache/storage URLs separate. Use Docker DNS locally
  and explicit provider endpoints in cloud; do not send internal traffic through ngrok.
- The demo uses one public origin. Separate frontend, API, Auth or CDN domains are
  possible, but require explicit browser URLs, CORS/CSP, cookies and redirect settings.
  `PUBLIC_URL` alone is not a generic multi-domain configuration system.

## URL and callback inventory

The general inventory includes features an app may add; it does not mean every
integration is implemented in this demo.

| Cloud-stack URL | Source / owner |
| --- | --- |
| App callback: `PUBLIC_URL/auth/callback` | API configuration; local helper also updates the Keycloak client's redirect URI/web origin. |
| Keycloak public issuer: `PUBLIC_URL/identity/realms/workbench` | Local Compose. Cloud can use a separate OIDC issuer instead. Changing issuer changes the app's issuer+subject identity namespace; existing jobs may appear under the previous identity. Restore the old issuer to regain that identity; no automatic relinking occurs. |
| Google broker callback: `PUBLIC_URL/identity/realms/workbench/broker/google/endpoint` | Example for alias `google`; copy the exact URL from Keycloak and register it externally. |
| Apple broker callback | Integration-specific; Apple support is not bundled. See [SOCIAL_LOGIN.md](SOCIAL_LOGIN.md). |
| Browser API/report paths | Relative `/api/...`; reports pass through authenticated API access rather than exposing MinIO. |
| Internal OIDC/S3 endpoints | Local `OIDC_INTERNAL_URL` / `S3_ENDPOINT` remain Docker-network addresses. Cloud endpoints are configured independently. |


| Other reference | Configuration owner and behavior |
| --- | --- |
| Signup, invite, reset, magic-link, email-change redirects | Auth service plus app return URL/allowlist. Existing emails retain old links; request fresh ones after an origin change. |
| Realtime/WebSocket URL | Browser backend config; use secure `wss://` with HTTPS and preserve ingress upgrade handling. |
| Storage URLs, signed downloads, images | Use public storage/API URLs or an explicit public CDN domain. Review generated URLs and existing database-stored absolute links. |
| Payment success/cancel/portal return | Application integration, normally derived from the public origin. Browser return is not payment confirmation; use the provider's verified state/webhook. |
| Webhook/event receiver | Register the public API/function path with its provider; implement signature verification, duplicate delivery handling and retries. No generic webhook handler is bundled. |
| Connecting external accounts | Each integration has its own OAuth callback/scopes/token lifecycle; signing in with Google does not automatically grant Drive/Gmail access. |
| Logout and account revocation | Provider-specific logout/post-logout URLs and policy; app logout and upstream SSO logout are separate. |
| Share links, notifications, canonical/social preview URLs, manifests/service workers | Application-specific. Search imported code, templates and saved configuration for absolute URLs; the origin helper does not rewrite them. |
| CORS, CSP, cookie security, allowed hosts, proxy headers | Application/ingress deployment settings. Permit explicit destinations; preserve the external HTTPS scheme at trusted proxy boundaries. Do not trust arbitrary forwarded headers or disable protections to make a tunnel work. |
| Static build variables | An imported Vite/Next app may bake public URLs into its bundle. Rebuild when necessary. Never put privileged secrets in public/build variables. |

Provider callbacks must match their registered URLs, including scheme, host, prefix
and path. Keep separate local/dev/production clients and secrets where practical.
A stable demo hostname reduces registration churn. The helpers update this local
stack, not external dashboards, credentials, provider scopes or cloud resources.

## Share a local stack

Run `make up`, then in one terminal:

```sh
make tunnel
```

This reads only `WEB_PORT` from this project's `.env` (default 8080) and starts
ngrok forwarding to that loopback app port. Install/authenticate ngrok separately.
Inspect the command without starting anything with `python3 scripts/tunnel.py --dry-run`.
In a second terminal, configure the HTTPS origin shown by ngrok:

```sh
make tunnel-set URL=https://YOUR-DEMO-HOST
```

`make tunnel-set` updates local `.env`, recreates Compose services, updates the
workbench Keycloak client's callback/web origin, and clears local app sessions.
It does not update upstream identity-provider registrations. See
[SOCIAL_LOGIN.md](SOCIAL_LOGIN.md) for broker-specific requirements.

Use the configured public URL for both your browser and testers. Start a fresh
login after changing origin, and update external provider registrations as needed.
Only the web entry point is forwarded; keep database, raw gateway/Studio, storage
admin and Mailpit off the tunnel. For emailed links, local Mailpit is developer-only;
configure real SMTP or deliberately deliver the captured test link to the tester.
Local Keycloak and application email use [Mailpit](EMAIL.md); its inbox stays private.

Stop ngrok with Ctrl-C. Restore the localhost configuration separately:

```sh
make tunnel-set URL=http://localhost:8080
```

Stopping ngrok does not restore application URLs automatically. Existing links and
cookies do not migrate between hosts. Keep only synthetic development data in demos.

## Cloud deployment

Use the cloud platform's normal environment/secrets mechanism to supply the public
origin and service endpoints to the relevant containers. The local `make tunnel-set` helper operate on Compose; they are not cloud deployment commands.
When a cloud hostname changes, update its app/Auth configuration and external provider
registrations together, then redeploy/restart/rebuild the affected services as needed.
If Auth is hosted separately, its provider callback belongs to the Auth hostname;
the app return URL belongs to the frontend hostname.

For Fly/Render compute with Neon, Neon supplies Postgres, not this scaffold's complete
Auth/Storage/Realtime backend. Keep or replace those services deliberately; changing
a database URL does not move their callbacks or semantics. See [CLOUD.md](CLOUD.md).
Do not expose the entire Supabase Compose stack by treating it as one managed web
container. Its state, routing, backups and backend deployment need an explicit plan.

## Validation before sharing or changing environments

Check rendered public configuration and real browser/server network destinations;
complete login/logout, email return, cross-user access, private upload/download and
Realtime where present. Exercise real providers/webhooks separately using development
registrations. Verify generated absolute URLs contain the selected public host and
no internal/old backend address. Test from a second device or external connection.

Existing local smoke/browser scripts intentionally refuse nonlocal origins; they
are not public-tunnel or cloud tests. Passing localhost/OrbStack checks establishes
only those paths. A real external tunnel, Google/Apple flows and provider-hosted
runtime remain pending unless explicitly recorded in [VALIDATION.md](VALIDATION.md).

Sources: [ngrok forwarding](https://ngrok.com/docs/gateway/endpoints/agent-cli-quickstart),
[Supabase self-hosted OAuth](https://supabase.com/docs/guides/self-hosting/self-hosted-oauth),
[Keycloak brokering](https://www.keycloak.org/docs/latest/server_admin/#_identity_broker),
[Apple web return URLs](https://developer.apple.com/documentation/signinwithapple/configuring-your-webpage-for-sign-in-with-apple).
