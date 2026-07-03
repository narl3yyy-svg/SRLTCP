#!/usr/bin/env bash
# SRLTCP quality gate — lint, type-check, tests
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -e ".[dev]"

echo "==> ruff"
ruff check srltcp tests

echo "==> mypy"
mypy srltcp

echo "==> pytest"
pytest tests/ -q --cov=srltcp --cov-report=term-missing --cov-fail-under=0

echo "All checks passed."