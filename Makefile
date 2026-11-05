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
	ngrok http 8080
tunnel-set:
	python3 scripts/set_public_url.py "$(URL)"
