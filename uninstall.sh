#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
purge=false; assume_yes=false; remove_image=false
while (($#)); do
  case "$1" in
    --purge) purge=true ;;
    --yes) assume_yes=true ;;
    --remove-image) remove_image=true ;;
    -h|--help) echo "Usage: ./uninstall.sh [--purge [--yes]] [--remove-image]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && docker compose down --remove-orphans || true
if $remove_image && command -v docker >/dev/null; then
  image="${IMAGE_NAME:-movie-ticket-watcher}:${IMAGE_TAG:-local}"
  [[ -f .env ]] && image="$(sed -n 's/^IMAGE_NAME=//p' .env | tail -1):$(sed -n 's/^IMAGE_TAG=//p' .env | tail -1)"
  docker image rm "$image" 2>/dev/null || true
fi
if $purge; then
  echo "Purge will permanently delete only:"
  printf '  %s\n' "$(pwd)/data" "$(pwd)/config" "$(pwd)/.env"
  if ! $assume_yes; then read -r -p "Type PURGE to continue: " answer; [[ "$answer" == PURGE ]] || { echo "Purge cancelled"; exit 1; }; fi
  rm -rf -- "$(pwd)/data" "$(pwd)/config"
  rm -f -- "$(pwd)/.env"
  echo "Application data and configuration purged. This cannot be recovered unless externally backed up."
else
  echo "Containers and project network removed. Preserved: $(pwd)/.env, $(pwd)/config, $(pwd)/data"
fi
