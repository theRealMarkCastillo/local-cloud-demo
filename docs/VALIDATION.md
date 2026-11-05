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

The local stack is left running at http://localhost:8080 with synthetic demo jobs.
The folder is a new Git repository on main; no commit, remote, or push was created.

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
