#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
backup="${1:?Usage: ./scripts/restore.sh data/backups/tickets-TIMESTAMP.db}"
[[ -f "$backup" ]] || { echo "Backup does not exist: $backup" >&2; exit 2; }
case "$(realpath "$backup")" in "$(pwd)/data/backups/"*) ;; *) echo "Restore file must be under data/backups" >&2; exit 2;; esac
docker compose stop worker
trap 'docker compose up -d worker >/dev/null 2>&1 || true' EXIT
docker compose run --rm --no-deps web python scripts/db_tool.py restore "/data/backups/$(basename "$backup")"
docker compose up -d web worker
for _ in {1..30}; do [[ "$(docker inspect --format '{{.State.Health.Status}}' movie-ticket-watcher-web 2>/dev/null)" == healthy ]] && break; sleep 2; done
docker compose exec web python scripts/healthcheck.py --web
trap - EXIT
