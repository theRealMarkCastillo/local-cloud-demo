# Email locally and in cloud environments

Mailpit is the default local SMTP sink. Keycloak sends identity emails; API/worker
code can send application messages through the same transport. Mailpit captures
messages instead of delivering them to real inboxes. No SMTP relay is configured.

## Common scaffold commands

All three repositories use `make email-config`, `make email-test` and
`make email-smoke`. The workflow is consistent; each backend owns its own identity
features and configuration names.

| Command | This cloud-stack scaffold | Supabase / Lovable scaffolds |
| --- | --- | --- |
| `email-config` | Apply local Keycloak realm SMTP/recovery settings | Apply local Supabase Auth/Mailpit configuration |
| `email-test` | Capture a synthetic application SMTP email | Capture a synthetic signup-confirmation email from Supabase Auth |
| `email-smoke` | Verify Keycloak email/reset links, new login and app SMTP capture | Verify confirmation and backend password recovery, new-password login and old-password rejection |

The Supabase demos do not bundle a password-reset UI or a generic app-notification
transport. Their recovery command checks Auth APIs; derived apps must implement
recovery UX and any additional server-side notification adapter deliberately.

## Local workflow

```sh
make up              # Starts Mailpit and applies realm SMTP settings safely
make email-test      # Sends a synthetic app message to demo@example.test
make email-smoke     # Local-only verification/reset/application email exercise
```

Open the inbox at **http://localhost:18026** (or your configured `MAIL_PORT`).
SMTP is internal at `mailpit:1025`; port 1025 is not published to the host. Mailpit
messages persist in the project-scoped `mailpit-data` volume, capped at 500 messages.
Stopping containers retains that volume. The inbox contains sensitive action links;
keep it off ngrok and don't share its contents or full token-bearing URLs in logs.
OrbStack-generated service hostnames are also local admin surfaces, not demo links.

`make up` / `make dev` apply only the local workbench realm's SMTP configuration and
forgot-password switch. Realm JSON import alone does not update existing Keycloak
volumes. `make email-config` reapplies those settings without resetting users, client
registrations or password/verification policy. It deliberately replaces any custom
SMTP configuration on that local realm with Mailpit; manage cloud IdP settings
separately. Admin CLI credentials stay in a temporary private container file and
are removed after use. No new public admin route is added.

The seeded demo users remain verified, and self-registration remains disabled.
The login page now offers password recovery. To require verification in a derived
project, deliberately choose the realm verification policy or a per-user
`VERIFY_EMAIL` required action; enabling SMTP alone does not enforce verification.
The email smoke creates its own unverified synthetic user, completes verification
and reset, checks login with the new password, then deletes only that user. Captured
messages remain local. The smoke refuses ngrok/cloud origins; use a disposable
localhost stack rather than reconfiguring or testing recovery on existing accounts.

## Application transport contract

| Setting | Local Compose default | Cloud / other SMTP service |
| --- | --- | --- |
| `SMTP_HOST` | `mailpit` | Provider host; empty/unset disables app email outside Compose |
| `SMTP_PORT` | `1025` | Usually 587 with STARTTLS or 465 with implicit TLS; follow provider settings |
| `SMTP_SECURITY` | `none` | `starttls` or `tls`; plaintext refused outside local environment |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Empty | Both supplied together through secrets, if the provider requires authentication |
| `SMTP_FROM` | `workbench@example.test` | Verified sender/domain configured with the provider |
| `MAIL_PORT` | `18026` | Local inbox mapping only; not an application or cloud SMTP setting |

`app.email.send_email(recipient, subject, text)` is a small synchronous, plain-text
SMTP transport using Python's standard library and verified TLS for secure modes.
Use it in worker code or a thread pool from asynchronous API code. It does not log
message bodies, recipients or credentials. The CLI example refuses any target other
than local Mailpit; reusable transport calls follow the configured server settings.
The API does not expose an unauthenticated arbitrary-email endpoint.

No job-completion mail is sent automatically. For durable notifications, add a
transactional outbox, retries and delivery deduplication deliberately. SMTP delivery
can be ambiguous after a timeout; blindly retrying may duplicate messages. This
transport does not implement templates, queueing, bounce handling or exactly-once
sending. Keycloak continues to own account tokens/templates and their expiry.

## Tunnels and cloud

Action links use the configured public identity/app URLs. After changing the public
origin, request fresh mail; previously captured messages keep old URLs. Mailpit stays
local even when the app is publicly reachable. For remote testers to receive mail,
configure a real SMTP provider/sandbox with deliberate recipient policy.

Keycloak's SMTP configuration and the API/worker's SMTP settings are separate
consumers: setting application variables does not configure a hosted IdP. Configure
that IdP's sender, TLS/auth and account-email policies through its supported admin
mechanism. Provider account emails from Google/Apple are also outside Mailpit.

Cloud deployments do not include Mailpit. Supply the application SMTP settings as
runtime environment/secrets on the services that send mail, and configure sender-domain
verification with the mail provider. On Render, add these optional variables to the
appropriate environment group/services; on Fly, import them as per-app secrets.
Do not copy local Mailpit hostnames/plaintext settings or example sender addresses to
production. Cloud SMTP/provider and deliverability validation remain pending.

See [URLs and callbacks](URLS_AND_CALLBACKS.md), [security](SECURITY.md) and
[cloud deployment](CLOUD.md).

Sources: [Mailpit configuration](https://mailpit.axllent.org/docs/configuration/),
[Mailpit health checks](https://mailpit.axllent.org/docs/integration/healthcheck/),
[Keycloak email and account actions](https://www.keycloak.org/docs/26.8.1/server_admin/).
