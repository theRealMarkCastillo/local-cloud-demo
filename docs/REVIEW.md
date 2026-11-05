# Review fixes — 2026-11-05

The P1 review finding is resolved in the source accompanying this report.
No cloud deployment has occurred. Review baseline: 36f650d. Scope covered authored code,
configuration and docs; unchanged upstream/vendor code, dependency advisories and
cloud runtime were excluded. Entire checkpoint history is not configured here.

## Plan and resolution

**P1 — delayed requests restoring revoked sessions: fixed.** The original middleware
unconditionally rewrote its session snapshot after a handler returned. A synthetic
ASGI reproduction delayed an authenticated request until after logout and proved that
it recreated the deleted ID. The new middleware uses atomic Redis Lua operations:

1. Create a random session ID and revocable family together.
2. Refresh TTL without rewriting state; compare-and-set state mutations against the
   exact snapshot, including nested changes.
3. Rotate the ID within the same family after login; revoke that family's current ID
   on logout, including a rotation completed while logout was in flight.
4. Emit cookies only on creation/rotation or actual logout. Read-only/stale responses
   cannot restore an earlier browser cookie. Cookies last for the browser session;
   Redis retains the one-hour sliding idle timeout.

Old-format Redis sessions intentionally require a fresh login. Redis family keys
expire alongside session keys; the local origin helper clears both key types.
These scripts require an EVAL-capable standalone-style Redis endpoint; Redis Cluster
and restricted HTTP-only services were not validated. Logout prevents future session
reuse; it cannot retract data already returned or cancel an authorized in-flight
handler. Provider SSO logout remains a separate integration.

## Verified outcomes

- `make check` and the final image rebuild passed.
- **7 deterministic real-Redis ASGI tests passed** (`make test`): delayed read/logout,
  stale mutation/logout, newer-state preservation, rotation after logout, logout
  overlapping completed rotation, stale-cookie avoidance and nested-state updates.
  Tests use random synthetic keys and remove only their own keys.
- A separate disposable localhost stack passed real OIDC login, CSRF/origin rejection,
  Postgres job → worker → private object storage → authenticated download, two-user
  isolation and sequential logout. Its six concurrency tests also passed before the
  additional nested-state regression was added. That stack and volumes were removed.
- The existing stack's ngrok origin was preserved. A normal local smoke refused its
  nonlocal URL; forcing an alternate host failed because cookies/callbacks belong
  to the configured origin. Those attempts were not counted as validation passes.
  The disposable stack provided isolated runtime validation without changing origins.

All checks used synthetic users/data. No cloud deployment, production data change or
push occurred. Full dependency/security audits, Google/Apple provider login and cloud
provider runtime remain pending. Documentation also corrects publication status,
records shared repo-selection guidance, and describes buffered report downloads.
