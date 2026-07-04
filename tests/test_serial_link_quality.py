"""Serial link quality estimation tests."""

from __future__ import annotations

from srltcp.transports.serial import SerialTransport


def test_high_rtt_lowers_link_quality() -> None:
    transport = SerialTransport(port="/dev/null")
    for _ in range(5):
        transport.record_ping_success(554.0)
        transport.link_quality_pct()
    quality = transport.link_quality_pct()
    assert quality < 70.0


def test_low_rtt_high_link_quality() -> None:
    transport = SerialTransport(port="/dev/null")
    for _ in range(5):
        transport.record_ping_success(40.0)
        transport.link_quality_pct()
    quality = transport.link_quality_pct()
    assert quality >= 90.0


def test_link_quality_smoothing_reduces_jitter() -> None:
    transport = SerialTransport(port="/dev/null")
    samples = []
    for rtt in (200.0, 450.0, 210.0, 430.0, 205.0, 440.0):
        transport.record_ping_success(rtt)
        samples.append(transport.link_quality_pct())
    assert max(samples) - min(samples) <= 25.0