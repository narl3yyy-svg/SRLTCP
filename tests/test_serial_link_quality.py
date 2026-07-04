"""Serial link quality estimation tests."""

from __future__ import annotations

from srltcp.transports.serial import SerialTransport


def test_high_rtt_lowers_link_quality() -> None:
    transport = SerialTransport(port="/dev/null")
    transport.record_ping_success(554.0)
    quality = transport.link_quality_pct(rtt_ms=554.0)
    assert quality < 70.0


def test_low_rtt_high_link_quality() -> None:
    transport = SerialTransport(port="/dev/null")
    transport.record_ping_success(40.0)
    quality = transport.link_quality_pct(rtt_ms=40.0)
    assert quality >= 90.0