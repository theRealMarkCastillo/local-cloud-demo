# Validation record — 2026-11-05

Executed against this standalone stack on the local Docker engine:

- Both base and hot-reload Compose configurations parse successfully.
- Application images build with the exact installed versions in `backend/requirements.lock`.
- Postgres and Keycloak databases, Redis, MinIO, Keycloak, API, worker, and web start.
  The one-shot initializer exits successfully and repeated initialization preserves data.
- `python3 scripts/smoke.py`: real Keycloak authorization-code login, anonymous access
  rejection, CSRF/origin rejection, job submission, worker completion, private object
  retrieval, second-user ownership isolation, and logout/session invalidation pass.
- Worker recovery: stop this project's worker, submit a job, delete only its Redis
  wake-up notification, restart worker; Postgres polling completes the pending job.
- Headless Chrome: sign in, submit a job, display completion, and sign out; no
  JavaScript errors. Desktop (1280px) and mobile (390px) layouts inspected; no
  horizontal overflow at mobile width.
- Public-origin helper: switch to `http://127.0.0.1:8080`, rerun login/job smoke,
  then restore `http://localhost:8080`. No actual external tunnel was opened.
- Backend compilation and `pip check` pass. Compilation uses a temporary cache
  because application code is intentionally not writable by the runtime user.
- Fly CLI `config validate` accepts all three cloud/fly configurations.
- Render Blueprint validates against `https://render.com/schema/render.yaml.json`.
- Git ignores populated `.env`, `.env.cloud`, and `.env.worker` files.

No cloud applications, identity providers, databases, Redis services, buckets, or
DNS records were provisioned. No cloud deployment or runtime validation occurred.
A real HTTPS tunnel, cloud OIDC provider, Neon TLS connection, external Redis, and
cloud S3 provider still need environment-specific validation before use.

The initial validation used http://localhost:8080 with synthetic demo jobs.
The later OrbStack section records the explicitly selected HTTPS origin. The source
is now published in the private [GitHub repository](https://github.com/theRealMarkCastillo/local-cloud-demo);
its reviewed main commit is 36f650d. Subsequent review fixes and documentation are recorded alongside this report.

## Documentation follow-up — 2026-11-05

Added social-login setup/verification guidance and the reusable stack/module map;
linked them from README, security, and cloud guidance. This follow-up changes no
runtime configuration, installs no identity-provider extension, registers no OAuth
clients, and provisions no resources. Google/Apple, real HTTPS tunnels, account
linking and upstream logout remain untested. Documentation links and whitespace
were checked; application tests were not repeated for this documentation-only edit.

## OrbStack login fix — 2026-11-05

The user opened https://web.local-cloud-demo.orb.local while the configured origin
was http://localhost:8080. Updated the local configuration and Keycloak callback to
the user's HTTPS origin. Sign-in now redirects to the configured canonical origin
before creating OIDC state, and the frontend identifies an alternate origin.
Browser sign-in, job completion and logout passed at the exact OrbStack URL.
The smoke script now reads PUBLIC_URL and verifies OrbStack TLS using its existing
public CA certificate, rather than disabling certificate validation.

## Review fixes — 2026-11-05

The session revocation finding is fixed locally. See [REVIEW.md](REVIEW.md) for the
implementation plan, concurrency cases and precise runtime scope. Seven deterministic
Redis/ASGI tests pass via `make test`; image rebuild and Compose validation pass.
Real OIDC/CSRF/worker/private-report/two-user/logout smoke passed in a disposable
localhost stack, which was removed with its volumes. The existing ngrok origin and
application data were preserved. Its direct smoke is not counted as passing because
the helper refuses nonlocal targets and alternate hosts have incompatible cookies.

Application code, tests and documentation include the review fixes described above.
No cloud deployment or production data change occurred. The browser cookie
now has browser-session lifetime while Redis enforces the one-hour sliding idle
expiry; sessions in the previous Redis format require fresh sign-in. Cloud runtime
and provider integration limits above still apply.

## URL and callback follow-up — 2026-11-05

Added the cross-environment [URL/callback contract](URLS_AND_CALLBACKS.md), including
external registrations, internal endpoints, cloud differences and imported-app limits.
`make tunnel` now reads the configured local web port; the Supabase repos also provide
`make tunnel-set` as an alias for their origin action. Dry-run checks covered the
project port, a custom port and invalid-port refusal without launching ngrok. No
running origin, external registration or cloud deployment was changed. Public-tunnel
and social-provider runtime validation remain pending.

## Mailpit and SMTP — 2026-11-05

- Added pinned Mailpit v1.32.0 with an internal-only SMTP listener, loopback inbox
  on port 18026 by default, health check and project-scoped persistent message volume.
- `make check up email-test` passed on the existing local stack. The local realm's
  SMTP/recovery configuration was updated without resetting users; the existing
  ngrok origin was preserved. The synthetic application message was present in the
  Mailpit inbox. No real external email was delivered.
- A disposable localhost stack passed `make check test smoke email-smoke`:
  **11 tests** (7 session/concurrency plus 4 SMTP/TLS/recipient regressions), real
  OIDC/CSRF/job/worker/private-report/two-user/logout smoke, Keycloak verification
  email and completed verification, forgot-password email/reset, fresh OIDC login
  with the new password, and application SMTP capture. The synthetic user, test
  stack and volumes were removed. Existing project data was not reset.
- The email smoke matches browser localhost Secure-cookie behavior, follows the
  explicit cross-browser email confirmation and resolves relative recovery links.
  This does not weaken application cookies or production TLS configuration.
- Backend/script Python syntax, documentation link targets and diff checks passed.
  Real SMTP provider delivery, cloud runtime and ngrok email-return flows remain
  pending. See [EMAIL.md](EMAIL.md) for local commands, verification policy and the
  separate application/hosted-identity SMTP configuration.

These Mailpit changes are recorded in the source accompanying this report.
No cloud deployment or new public tunnel was performed.

The email guide and README also document the three shared `email-*` command names
across all scaffolds. Supabase implementations use native Auth confirmation/recovery
APIs; command consistency does not imply identical configuration variables, a bundled
application notification transport or a password-reset UI in those demos.
