# Contributing to SRLTCP

Thank you for helping improve SRLTCP. This document covers local development, quality gates, and how to submit changes.

## Prerequisites

- Python **3.12+**
- Linux, macOS, or Windows (WSL recommended on Windows)
- Optional: **JDK 17**, Android SDK API 34, Python 3.12 for [local APK builds](android/README.md)

## Quick Start

```bash
git clone https://github.com/narl3yyy-svg/SRLTCP.git
cd SRLTCP
./run.sh web --debug
```

Open the URL printed in the terminal (typically `https://127.0.0.1:9876`).

### Test hub mode locally

```bash
# Terminal 1 — hub
./run.sh hub --port 7825

# Terminal 2 & 3 — clients (different names)
./run.sh web --name alice
./run.sh web --name bob
```

In each client: **Settings → Network** → enable hub → `127.0.0.1:7825` → Save → **Announce** → trust and chat.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the full quality gate (matches GitHub Checks workflow):

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
| `srltcp/core/` | Messaging, handshake, transfers, discovery, hub |
| `srltcp/core/messaging/hub.py` | Hub client + server forwarding |
| `srltcp/transports/` | TCP and serial transports |
| `srltcp/web/` | HTTPS web UI and static assets |
| `srltcp/routes/` | REST API handlers |
| `android/` | Gradle + Chaquopy Android app |
| `scripts/` | `check.sh`, `build-android.sh`, `sync-android-python.sh` |
| `.github/workflows/checks.yml` | CI: lint + typecheck + tests |
| `tests/` | Pytest suite |

## Coding Standards

- **Python**: type hints on public functions, `ruff` clean, prefer explicit error handling over bare `except`.
- **Security**: never log session keys, private keys, or decrypted payloads at INFO level. See [SECURITY.md](SECURITY.md).
- **UI**: match existing CSS variables and component patterns in `app.css` / `app.js`.
- **Tests**: add regression tests for bugs you fix; use `pytest-asyncio` for async code.

## Pull Requests

1. Fork and create a feature branch from `main`.
2. Keep changes focused — one logical fix or feature per PR.
3. Update `srltcp/RELEASE_NOTES.md` for user-visible changes.
4. Bump `__version__` in `srltcp/__init__.py` only when preparing a release (maintainers may do this).
5. Ensure `bash scripts/check.sh` passes (same as GitHub Checks).
6. Fill out the PR template completely.

## Android Builds

APKs are built **locally**, not in GitHub Actions.

```bash
export ANDROID_HOME="$HOME/Android/Sdk"
bash scripts/build-android.sh
```

See [android/README.md](android/README.md). Run `bash scripts/sync-android-python.sh` before Gradle whenever you change Python code.

## GitHub / CI

- **Checks** runs on every push and pull request to `main`.
- There is **no** automated APK workflow — intentional, to avoid Buildozer/p4a fragility.
- Do not re-add cloud APK builds without maintainer discussion and a reproducible Gradle path.

## Questions

Open a [GitHub Discussion](https://github.com/narl3yyy-svg/SRLTCP/discussions) or issue for non-security questions. Security issues: see [SECURITY.md](SECURITY.md).