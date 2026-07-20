.PHONY: build up down restart logs logs-web logs-worker test doctor simulate backup restore update
build:
	docker compose build
up:
	docker compose up -d
down:
	docker compose down
restart:
	docker compose restart
logs:
	docker compose logs -f
logs-web:
	docker compose logs -f web
logs-worker:
	docker compose logs -f worker
test:
	docker compose run --rm test
doctor:
	docker compose exec web python scripts/doctor.py
simulate:
	docker compose run --rm --no-deps web python scripts/seed_demo.py
backup:
	./scripts/backup.sh
restore:
	@test -n "$(FILE)" || (echo 'Use: make restore FILE=data/backups/file.db'; exit 2)
	./scripts/restore.sh "$(FILE)"
update:
	./update.sh
