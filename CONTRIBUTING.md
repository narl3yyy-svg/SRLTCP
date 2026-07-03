# Contributing to SRLTCP

Thank you for helping improve SRLTCP. This document covers local development, quality gates, and how to submit changes.

## Prerequisites

- Python **3.12+**
- Linux, macOS, or Windows (WSL recommended on Windows)
- Optional: Android Studio + JDK 17 for APK builds

## Quick Start

```bash
git clone https://github.com/narl3yyy-svg/SRLTCP.git
cd SRLTCP
./run.sh web --debug
```

Open the URL printed in the terminal (typically `https://127.0.0.1:9876`).

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the full quality gate:

```bash
bash scripts/check.sh
```

Or individually:

```bash
ruff check srltcp tests
mypy srltcp
pytest tests/ -v
```

## Pre-commit (recommended)

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Project Layout

| Path | Purpose |
|------|---------|
| `srltcp/core/` | Messaging, handshake, transfers, discovery |
| `srltcp/transports/` | TCP and serial transports |
| `srltcp/web/` | HTTPS web UI and static assets |
| `srltcp/routes/` | REST API handlers |
| `android/` | Chaquopy Android wrapper |
| `tests/` | Pytest suite |

## Coding Standards

- **Python**: type hints on public functions, `ruff` clean, prefer explicit error handling over bare `except`.
- **Security**: never log session keys, private keys, or decrypted payloads at INFO level.
- **UI**: match existing CSS variables and component patterns in `app.css` / `app.js`.
- **Tests**: add regression tests for bugs you fix; use `pytest-asyncio` for async code.

## Pull Requests

1. Fork and create a feature branch from `main`.
2. Keep changes focused — one logical fix or feature per PR.
3. Update `srltcp/RELEASE_NOTES.md` for user-visible changes.
4. Bump `__version__` in `srltcp/__init__.py` only when preparing a release (maintainers may do this).
5. Ensure `bash scripts/check.sh` passes.
6. Fill out the PR template completely.

## Android Builds

See [android/README.md](android/README.md). Always rsync `srltcp/` into `android/app/src/main/python/srltcp/` before Gradle sync.

## Questions

Open a [GitHub Discussion](https://github.com/narl3yyy-svg/SRLTCP/discussions) or issue for non-security questions.