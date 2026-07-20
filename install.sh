#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Installation failed at line $LINENO" >&2' ERR
cd "$(dirname "${BASH_SOURCE[0]}")"

port=8787; pull=false; no_start=false
while (($#)); do
  case "$1" in
    --port) port="${2:?missing port}"; shift 2 ;;
    --build) shift ;;
    --pull) pull=true; shift ;;
    --no-start) no_start=true; shift ;;
    -h|--help) echo "Usage: ./install.sh [--port 8787] [--build] [--pull] [--no-start]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$port" =~ ^[0-9]+$ ]] && ((port > 0 && port < 65536)) || { echo "Invalid port: $port" >&2; exit 2; }
command -v docker >/dev/null || { echo "Docker is not installed. Install Docker Engine explicitly, then retry." >&2; exit 1; }
docker info >/dev/null || { echo "Docker daemon is unavailable or your user lacks permission." >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 ('docker compose') is required." >&2; exit 1; }

mkdir -p data/screenshots data/logs data/backups config
if [[ ! -f .env ]]; then cp .env.example .env; echo "Created .env"; else echo "Preserving existing .env"; fi

set_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp .env.XXXXXX)"
  awk -v key="$key" -v value="$value" 'BEGIN{done=0} $0 ~ "^"key"=" {print key"="value; done=1; next} {print} END{if(!done) print key"="value}' .env >"$tmp"
  chmod --reference=.env "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" .env
}
set_env HOST_PORT "$port"
set_env PUID "$(id -u)"
set_env PGID "$(id -g)"
if ! grep -qE '^SECRET_KEY=.+$' .env; then
  set_env SECRET_KEY "$(od -An -N48 -tx1 /dev/urandom | tr -d ' \n')"
  echo "Generated SECRET_KEY"
fi
chmod 600 .env

build_args=()
$pull && build_args+=(--pull)
docker compose build "${build_args[@]}"

if $no_start; then echo "Image built; services were not started (--no-start)."; exit 0; fi
docker compose up -d --build
healthy=false
for _ in {1..60}; do
  if [[ "$(docker inspect --format '{{.State.Health.Status}}' movie-ticket-watcher-web 2>/dev/null || true)" == healthy ]] &&
     [[ "$(docker inspect --format '{{.State.Health.Status}}' movie-ticket-watcher-worker 2>/dev/null || true)" == healthy ]]; then healthy=true; break; fi
  sleep 2
done
if ! $healthy; then docker compose ps; docker compose logs --tail=100 web worker; echo "Services did not become healthy" >&2; exit 1; fi
docker compose exec web python scripts/doctor.py

echo "Web interface: http://127.0.0.1:$port"
docker compose ps
echo "Health: web and worker healthy"
echo "Data: $(pwd)/data (database, screenshots, logs, backups)"
echo "Configuration: $(pwd)/config and $(pwd)/.env"
echo "Logs: docker compose logs -f [web|worker]"
echo "Update: ./update.sh"
echo "Stop: docker compose down"
echo "Uninstall: ./uninstall.sh (preserves data)"
