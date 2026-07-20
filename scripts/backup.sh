#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose run --rm --no-deps web python scripts/db_tool.py backup
