#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
python_bin="${PYTHON_BIN:-$repo_dir/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin=python3
"$python_bin" -m ruff check .
"$python_bin" -m pytest -q
"$python_bin" -m compileall -q app scripts worker.py
bash -n install.sh uninstall.sh scripts/self-test.sh
echo "Self-test passed"
