# A reusable local-first application pattern

## Three modes, one application contract

| Mode | Compute and data | External dependencies |
| --- | --- | --- |
| Everyday local | Full Compose stack; local Keycloak users | Initial image/package downloads; none required for normal demo jobs afterward |
| Local shared demo | Same stack, one HTTPS tunnel entry point | Tunnel service; real identity providers only if configured |
| Hosted demo | Same web/backend Dockerfiles; separate worker command | Fly or Render, Neon, Redis, S3-compatible bucket, hosted OIDC provider |

Keep the deployment descriptions small and separate. Compose owns local service
names, volumes and readiness. Cloud manifests own provider placement, service
addresses and secret delivery. Application code owns the shared settings contract.
A changed database URL does not copy data, and a changed issuer does not migrate users.

## Responsibilities and replaceable boundaries

| Capability | Current implementation | Portability boundary |
| --- | --- | --- |
| Browser UI / ingress | Static frontend + nginx | Same-origin `/api` and `/auth`; no browser service secrets |
| Application API | FastAPI | Environment settings; self-contained image |
| Background work | Separate process, shared image | Durable Postgres jobs; idempotent report keys; at-least-once execution |
| Relational data | Postgres | SQL migrations; Neon TLS connection in cloud |
| Short-lived state | Redis | Server sessions + expendable worker wake-ups; not authoritative job data |
| Files / reports | Private MinIO bucket | S3 API and scoped cloud credentials |
| Authentication / SSO | Keycloak using OIDC | Stable issuer; separate client per app/environment |
| MCP tools | `mcp` service; Keycloak `mcp:tools` scope with audience `PUBLIC_URL/mcp` | Resource URL per MCP server; public PKCE client; no token passthrough; see `MCP.md` |
| Social login | Not configured | Keycloak brokering; see `SOCIAL_LOGIN.md` |
| Secrets | Ignored local file; cloud secret settings | Inject at runtime into only the service that needs them |

This covers a useful web/API/data/worker/storage/auth baseline, rather than every
requirement of every modern application. Keep optional capabilities out of the
mandatory local startup path until the application actually needs them.

## Optional modules to add deliberately

| Module | Local option | Hosted option / integration check |
| --- | --- | --- |
| Email | Local SMTP capture container, e.g. Mailpit | Transactional mail provider; verify delivery and links separately |
| Scheduled work / collectors | Dedicated scheduler or explicit commands | Singleton scheduling, retries, idempotency and shutdown policy |
| Observability | Structured logs; optional local OpenTelemetry collector | Telemetry backend, redaction, alerts and retention |
| Organizations / permissions | Seeded roles and tenants | Server-enforced authorization and invitations; authentication alone is insufficient |
| Long-running jobs | Leases, heartbeats and retry/dead-letter policy | Provider shutdown limits and worker availability |
| Payments / external APIs | Test fixtures and provider sandbox | Real callbacks through a tunnel and separate provider credentials |
| Recovery | Synthetic data, repeatable migrations | Backup retention and an actual restore drill |

These modules are **not included** in this scaffold. Add a Compose profile/override
for optional containers and a matching cloud contract when a module is implemented.
A fake or captured local service is useful for development, but it does not prove
that the real external integration works.

## Copying this scaffold into another project

1. Choose a distinct Compose project name so volumes/networks don't collide. The
   template currently sets `name: local-cloud-demo`; change it or use `-p` consistently.
2. Choose a separate public port/origin and update the exact identity callbacks.
3. Create dedicated credentials, Keycloak client/realm policy and data resources.
   Share an identity provider for SSO when desired, while keeping app clients separate.
4. Replace the sample text-analysis job and version the schema changes. The current
   short transaction-held worker is an example, not a long-running collector design.
5. Preserve per-owner authorization and choose an explicit organization/role policy.
6. Keep the browser configuration public-only; use runtime secrets for server credentials.
7. Run the local smoke flow. Exercise the tunnel and actual cloud path when you need
   them, especially after auth, storage, networking or dependency changes.
8. Review and rename cloud services before provisioning; the sample names must not
   collide with unrelated deployments. The supplied cloud setup does not automatically stop all compute when idle.

See `VALIDATION.md` for executed checks and `CLOUD.md` for the unexecuted deployment
procedure. Local success and schema validation do not establish cloud runtime correctness.
