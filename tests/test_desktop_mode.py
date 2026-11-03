"""`srltcp web --desktop`, as the desktop app (desktop/main.js) runs it: one
ready line with the port, token and certificate fingerprint; a clean stop
when the app closes our stdin; a restart is an exit code for the app."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import srltcp.core  # noqa: F401  (import order: core before its parts)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_desktop_mode_reports_ready_and_stops_when_stdin_closes(tmp_path: Path) -> None:
    web, tcp, disc = _free_port(), _free_port(), _free_port()
    env = {**os.environ, "SRLTCP_DATA_DIR": str(tmp_path), "SRLTCP_DESKTOP": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "srltcp", "web", "--desktop", "--port", str(web),
         "--tcp-port", str(tcp), "--discovery-port", str(disc)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    assert proc.stdout is not None and proc.stdin is not None
    ready = None
    deadline = time.monotonic() + 60
    lines = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        if line.startswith("SRLTCP-DESKTOP "):
            ready = json.loads(line[len("SRLTCP-DESKTOP "):])
            break
    assert ready, "".join(lines[-20:])
    assert ready["port"] == web and len(ready["cert_sha256"]) == 64 and ready["token"]
    assert not any("token=" in x for x in lines)  # (no browser link printed)
    proc.stdin.close()  # the app quit
    assert proc.wait(timeout=30) == 0
    with socket.socket() as s:  # its ports are free again
        s.bind(("0.0.0.0", tcp))


def test_restart_under_the_desktop_app_is_an_exit_code(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import srltcp.web.server  # noqa: F401  (the app's import order: routes import web)
    from srltcp.routes import api

    def fake_exit(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setenv("SRLTCP_DESKTOP", "1")
    monkeypatch.setattr(api.os, "_exit", fake_exit)
    monkeypatch.setattr(api.os, "execv", lambda *_a: pytest.fail("execv under the desktop app"))
    with pytest.raises(SystemExit) as stop:
        api._start_again()
    assert stop.value.code == api.DESKTOP_RESTART_CODE
