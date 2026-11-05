# Standalone local/cloud scaffold

This folder is independent of ai-companions. Local Docker Compose application
validation is expected here. Never deploy or provision paid cloud resources without
an explicit request. Keep `.env*` secrets ignored, preserve owner-scoped job access,
OIDC validation, server-side sessions and CSRF checks. The worker is at-least-once.
Use the existing Dockerfiles in both local and cloud definitions. Validate local
changes with `docker compose config --quiet`, `make up`, and `make smoke`.
Cloud examples are optional and must not be described as deployed or runtime-tested
without evidence. Do not use real production data or expose admin/data service ports.
