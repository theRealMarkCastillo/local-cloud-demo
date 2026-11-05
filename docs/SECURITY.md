# Identity, secrets, and trust boundaries

## Use standards across environments

The application is an OIDC confidential web client using authorization code flow
with PKCE (S256), state, and nonce. Authlib verifies ID-token claims and signatures.
Local Keycloak is an identity provider, not part of the application's business
logic. In the cloud register an exact callback of `PUBLIC_URL/auth/callback` with
a hosted OIDC provider and configure its issuer/client ID/client secret.

SSO between apps comes from using the same identity provider, with **a separate
client registration for each app/environment**. Each application keeps its own
session cookie. Do not share client secrets or cookies between unrelated projects.
Users are identified by issuer + subject, not their mutable email address.

Discovery happens at API startup. The public issuer must match the discovery
issuer exactly. Only local Keycloak uses `OIDC_INTERNAL_URL` to reach token/JWKS
endpoints over the container network while leaving browser authorization URLs
public. Hosted providers normally need no internal override. Providers requiring
nonstandard authorization parameters or client authentication may need an explicit
adapter; compatibility with every provider is not claimed.

## External identities

Use Keycloak as the broker for Google/Apple so the application continues to trust
one OIDC issuer. Provider secrets stay at Keycloak; they are separate from the
API's OIDC client secret. Social providers, account linking and upstream logout
are not configured here. See [the social login guide](SOCIAL_LOGIN.md), including
issuer continuity and the provider integration test matrix.

## Sessions and authorization

- Browser gets an opaque HttpOnly, SameSite=Lax cookie. Cloud cookies are Secure
  and use the `__Host-` prefix. Tokens are not stored in localStorage or frontend code.
- OIDC state and app sessions live in Redis; session IDs rotate after login.
- Redis entries expire after one hour of inactivity. Redis loss signs users out.
- The API checks exact Origin and a session CSRF token for state-changing routes.
- Each job query and report download is scoped to the authenticated owner.
- Downloads stream through the API; the bucket is private and S3 keys stay server-side.
- App logout destroys its Redis session. It **does not** end the provider's SSO
  session. A subsequent sign-in may be automatic. Add provider-supported RP-initiated
  logout and account revocation handling if the product needs them.

The first page and health endpoint are intentionally public. Login is required for
job records, submissions, and reports. Keycloak admin routes are blocked by nginx.
There is no registration UI. To add another local user use the Keycloak admin CLI;
changing the import JSON only takes effect on an uninitialized realm.

## Secret source changes; application contract stays the same

| Environment | Secret delivery |
| --- | --- |
| Local | Ignored `.env`, file permissions 0600; Compose injects values |
| Fly | `fly secrets` per app; never Docker build arguments |
| Render | Secret environment settings / environment group; never committed YAML values |
| Larger organization | External secret manager and workload identity where supported |

Do not commit populated `.env` files, put secrets in frontend variables, bake them
into images, paste them into CI logs, or pass them as command-line literal values.
Environment injection is a reasonable baseline, not a complete secret-management
system: operators with runtime/Docker access can inspect environment secrets.
Compose `.env` is plaintext; a local ignored file is convenience, not a vault.

Use different credentials for local/demo/production and separate data resources.
Local MinIO root credentials are demo-only. Cloud S3 credentials should be scoped
to the report bucket/prefix. Use dedicated database roles; separate migration
privileges from application privileges when hardening a real deployment. Fly
requires importing secrets separately per app. Limit each service to the secrets
it needs; never give the web container database, OIDC client, or S3 credentials.
Rotate leaked credentials at the provider, update runtime secrets, redeploy, and
invalidate affected sessions. Never copy real production data into this demo.

## Before treating a derived project as production

This example demonstrates boundaries; it is not a comprehensive production system.
Add invitation/organization policy or role-based access, rate and storage quotas,
login-abuse protection, access/audit logs with sensitive values redacted, resource
limits, absolute session lifetime/revocation as needed, dependency/image scanning,
backup + restore checks, and operational monitoring. Review proxy headers for your
specific hosting chain. Cloud configuration requires HTTPS and TLS database/Redis
connections. Review OIDC discovery and cloud egress permissions as trusted config.

The synchronous demo Redis client is used in sync API routes; middleware offloads
it from the async event loop. OAuth callback access logs are disabled at nginx and uvicorn to avoid logging authorization codes. The worker
uses a short DB transaction around S3 writes for clarity; do not extend this pattern
to minutes-long collectors or non-idempotent external actions.
