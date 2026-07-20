#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Update failed; recent logs follow" >&2; docker compose logs --tail=100 web worker 2>/dev/null || true' ERR
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v docker >/dev/null && docker info >/dev/null
docker compose version >/dev/null
[[ -f .env ]] || { echo "Missing .env; run ./install.sh first" >&2; exit 1; }
mkdir -p data/backups
if [[ -f data/tickets.db ]]; then docker compose run --rm --no-deps web python scripts/db_tool.py backup; fi
docker compose build --pull
docker compose run --rm --no-deps web migrate
docker compose up -d --build --remove-orphans
for _ in {1..60}; do
  web="$(docker inspect --format '{{.State.Health.Status}}' movie-ticket-watcher-web 2>/dev/null || true)"
  worker="$(docker inspect --format '{{.State.Health.Status}}' movie-ticket-watcher-worker 2>/dev/null || true)"
  [[ "$web" == healthy && "$worker" == healthy ]] && break
  sleep 2
done
[[ "${web:-}" == healthy && "${worker:-}" == healthy ]] || exit 1
docker compose exec web python scripts/doctor.py
echo "Update complete; persistent data was preserved."
