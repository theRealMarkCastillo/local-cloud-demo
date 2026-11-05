.PHONY: dev up down logs check smoke tunnel tunnel-set
up:
	docker compose up --build -d --wait
dev:
	docker compose -f compose.yaml -f compose.dev.yaml up --build -d --wait
down:
	docker compose down
logs:
	docker compose logs -f --tail=50
check:
	docker compose config --quiet
smoke:
	python3 scripts/smoke.py
tunnel:
	python3 scripts/tunnel.py
tunnel-set:
	python3 scripts/set_public_url.py "$(URL)"

.PHONY: test
test:
	docker compose run --rm --no-deps -T -v "$(CURDIR)/backend/app:/srv/app:ro" -v "$(CURDIR)/backend/tests:/srv/tests:ro" api python -m unittest discover -s /srv/tests -v
