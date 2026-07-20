#!/usr/bin/env bash
set -Eeuo pipefail

puid="${PUID:-1000}"
pgid="${PGID:-1000}"
[[ "$puid" =~ ^[0-9]+$ && "$pgid" =~ ^[0-9]+$ ]] || { echo "PUID and PGID must be numeric" >&2; exit 64; }
[[ "$(id -u)" != 0 ]] || { echo "Refusing to run the application as root" >&2; exit 77; }
for directory in /data /data/screenshots /data/logs /data/backups /config; do
  mkdir -p "$directory" 2>/dev/null || { echo "Cannot write $directory as uid=$(id -u) gid=$(id -g); fix host ownership" >&2; exit 73; }
done

umask 0027
python -c 'from app.database import init_db; init_db()'

case "${1:-web}" in
  web) exec uvicorn app.main:app --host "${APP_BIND:-0.0.0.0}" --port "${APP_PORT:-8787}" --proxy-headers ;;
  worker) exec python /app/worker.py ;;
  migrate) exit 0 ;;
  test) exec pytest -p no:cacheprovider "${@:2}" ;;
  doctor) exec python /app/scripts/doctor.py "${@:2}" ;;
  *) exec "$@" ;;
esac
