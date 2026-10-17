"""The speed and time left shown for a transfer."""

from __future__ import annotations

from pathlib import Path

import srltcp.core  # noqa: F401  (import order: core before messaging)
from srltcp.core.messaging import transfer as transfer_mod
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.models import FileTransfer, TransferState


def _transfer(size: int) -> FileTransfer:
    return FileTransfer(
        id="0" * 32, sender_hash="a" * 32, recipient_hash="b" * 32, filename="f",
        path=Path("f"), size=size, sha256="", transport="serial",
        state=TransferState.TRANSFERRING,
    )


def test_slow_speeds_are_not_rounded_away() -> None:
    t = _transfer(10 * 1024 * 1024)
    t.speed_bps = 3 * 1024  # a serial radio
    t.offset = 1024 * 1024
    d = t.to_dict()
    assert d["speed_bps"] == 3072
    assert d["eta_s"] == round(9 * 1024 * 1024 / 3072)
    t.speed_bps = 0
    assert t.to_dict()["eta_s"] is None


def test_speed_is_measured_over_a_window_of_seconds() -> None:
    """Confirmations every few seconds (a slow radio) still give a steady
    speed; it falls to zero when nothing moves for longer than the window."""
    backend = MessagingBackend(NodeConfig(name="n"))
    t = _transfer(1024 * 1024)
    now = 1000.0
    backend._measure_speed(t, now)
    for step in range(1, 7):  # 6 KB confirmed every 2 s: 3 KB/s
        t.offset = step * 6 * 1024
        backend._measure_speed(t, now + 2 * step)
    assert abs(t.speed_bps - 3 * 1024) < 1
    for idle in range(1, 12):  # then nothing for 11 s
        backend._measure_speed(t, now + 12 + idle)
    assert t.speed_bps == 0
    t.offset = 0  # resent from the last confirmed byte: starts afresh
    backend._measure_speed(t, now + 30)
    assert t.speed_bps == 0
    assert transfer_mod.SPEED_WINDOW >= 5
