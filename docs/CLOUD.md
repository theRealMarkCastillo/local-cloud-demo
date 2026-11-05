# Optional cloud deployment

Nothing here deploys automatically. These are optional manifests for the same
application Dockerfiles; provision cloud resources only when you need them.
Do not upload Compose volumes or the local `.env` to a hosting provider.

## Shared preparation

1. Create a separate Neon project/database for this app. Use a direct TLS connection
   initially (`sslmode=verify-full` where supported; `require` is accepted). The demo
   opens short connections; add a pool for real traffic. If adopting Neon's pooled
   endpoint, verify driver/session semantics and use a direct URL for migrations.
2. Provide a Redis endpoint with TLS (`rediss://`). This example uses blocking pop,
   lists, and server sessions: verify those commands with the chosen Redis service.
3. Create a **private** S3-compatible bucket and scoped application credentials.
   The MinIO Python client uses ordinary S3 APIs, so local MinIO is replaced by
   hosted object storage. Set endpoint, region, bucket, access key and secret.
4. Configure a hosted OIDC provider. For Keycloak, use a production deployment with
   persistent database, HTTPS, backups, and admin access controls. **Do not deploy
   the Compose `start-dev` identity container as production SSO.**
5. Choose the web hostname; register exactly `https://HOST/auth/callback`. Set
   `PUBLIC_URL` to that web origin for API and worker. Omit `OIDC_INTERNAL_URL`.
6. Run schema migrations once before new API/worker code. Provision the cloud
   bucket separately; never run `app.bootstrap` in the cloud.

`.env.cloud.example` documents the common runtime contract. Filled files are ignored.
Changing the database URL does not migrate local data. Start a fresh cloud demo with
synthetic submissions, or design a separate deliberate data migration.

## Render + Neon

Use `cloud/render.yaml` as the Blueprint path with repository root as build context.
The Blueprint creates three paid app services: public web, private API, worker.
Neon, Redis, identity and S3 are external and are not provisioned by the Blueprint.

- Rename services/env group if creating multiple examples. The Blueprint must not
  reuse names from an existing unrelated deployment.
- Populate the backend data secret environment group and API-only identity group. `sync: false` values need
  setting in Render's environment-group UI; do not assume Blueprint sync prompts
  for every group value.
- Set `PUBLIC_URL` after choosing the web URL and configure OIDC before API startup.
- The API's `preDeployCommand` runs `python -m app.migrate`.
- Deploy API/migration first, then worker, then web. Auto deploys are disabled to
  keep schema/code ordering explicit. The web gets only private API host/port.
- OIDC-only secrets are in the API-only identity group;
  the worker needs DB, Redis and S3, not OIDC credentials.

Suspend/delete optional app services when finished; stopping the web alone does
not stop paid worker/API compute. External providers have their own billing.

## Fly + Neon

`cloud/fly/{api,worker,web}.toml` define three apps in the same Fly organization.
Choose a unique prefix, e.g. `my-workbench`. Create these apps explicitly:

```sh
fly apps create my-workbench-api
fly apps create my-workbench-worker
fly apps create my-workbench-web
```

Edit `cloud/fly/web.toml`: set **both** upstream values to
`my-workbench-api.internal:8000`. Secrets on API/worker must set `PUBLIC_URL` to
`https://my-workbench-web.fly.dev`. Configure the OIDC callback at the provider.
From a populated, ignored `.env.cloud` (never include shell syntax):

```sh
fly secrets import --stage --app my-workbench-api < .env.cloud
# Prepare .env.worker with only ENVIRONMENT, PUBLIC_URL, DB, Redis and S3 values.
fly secrets import --stage --app my-workbench-worker < .env.worker
fly deploy --app my-workbench-api --config cloud/fly/api.toml --ha=false
fly deploy --app my-workbench-worker --config cloud/fly/worker.toml --ha=false
fly deploy --app my-workbench-web --config cloud/fly/web.toml --ha=false
```

Run from repository root. Commands shown here are **manual deployment steps**, not
something `make up` executes. API deploy runs the versioned schema release step;
worker/web deploys do not. No secrets go into web. Fly imports are per app, not
organization-wide. Review plans/regions and resulting counts before use.

Web may autostop; API is intentionally always on because `.internal` traffic does
not provide the same wake-on-request behavior as Fly Proxy. Worker is also always
on: a Redis notification doesn't automatically start a stopped worker. This template
chooses explicit behavior over promising a sleeping full stack. For cheaper dev,
stop the three apps deliberately when done, or add a tested Flycast/wake strategy.
Scaling workers to zero does not delete Neon data or the S3 bucket.

## Social identity providers

If Keycloak brokers Google/Apple, the application still points at Keycloak's issuer.
Register social-provider callbacks at the broker's hosted URL, and register the
application callback separately on the Keycloak application client. Configure
provider secrets in the broker's own deployment, not in the web/worker containers.
See [SOCIAL_LOGIN.md](SOCIAL_LOGIN.md) before changing issuers, linking accounts,
or moving from a local tunnel to a hosted identity provider.

## Validation boundary

Local Compose verifies local images and behavior. Cloud manifests require validation
with the installed provider CLI and an actual cloud smoke run before they can be
considered verified deployments. In particular, verify private DNS, TLS, identity
callbacks, migration permissions, graceful shutdown, and object-storage access.

Provider references:
- https://render.com/docs/blueprint-spec
- https://render.com/docs/docker
- https://fly.io/docs/reference/configuration/
- https://fly.io/docs/networking/private-networking/
- https://neon.com/docs/connect/connect-from-any-app
- https://www.keycloak.org/server/hostname
- https://www.keycloak.org/server/reverseproxy
