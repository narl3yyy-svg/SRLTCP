"""Incoming calls ring until answered/declined, and reach a page opened late."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.node import SRLTCPNode
from srltcp.core.settings import AppSettings

PEER = "a" * 32


def node(tmp_path: Path) -> SRLTCPNode:
    return SRLTCPNode(NodeConfig(name="me", incoming_dir=str(tmp_path / "in")), AppSettings())


def ev(signal: dict) -> dict:
    return {"kind": "call_signal", "hash_id": PEER, "signal": signal}


def test_offer_rings_and_is_kept_for_a_late_page(tmp_path: Path) -> None:
    n = node(tmp_path)
    with patch("srltcp.utils.android_notify.ring_android_call") as ring:
        offer = {"type": "offer", "call_id": "c1", "video": True, "sdp": "x"}
        n.track_call_signal(PEER, offer, event=ev(offer))
        ring.assert_called_once()
        assert ring.call_args.args[1] is True  # video
    ice = {"type": "ice", "call_id": "c1", "candidate": {}}
    n.track_call_signal(PEER, ice, event=ev(ice))
    assert [e["signal"]["type"] for e in n.pending_call_events()] == ["offer", "ice"]


def test_answer_here_or_caller_hangup_stops_ringing(tmp_path: Path) -> None:
    n = node(tmp_path)
    offer = {"type": "offer", "call_id": "c1", "sdp": "x"}
    for end, incoming in (({"type": "answer", "call_id": "c1"}, False),
                          ({"type": "hangup", "call_id": "c1"}, True)):
        n.track_call_signal(PEER, offer, event=ev(offer))
        with patch("srltcp.utils.android_notify.stop_android_ringing") as stop:
            n.track_call_signal(PEER, end, event=ev(end) if incoming else None)
            stop.assert_called_once()
        assert n.pending_call_events() == []


def test_other_call_ids_do_not_stop_ringing(tmp_path: Path) -> None:
    n = node(tmp_path)
    offer = {"type": "offer", "call_id": "c1", "sdp": "x"}
    n.track_call_signal(PEER, offer, event=ev(offer))
    n.track_call_signal(PEER, {"type": "hangup", "call_id": "old"}, event=None)
    assert n.pending_call_events()


async def test_decline_from_notification_tells_caller(tmp_path: Path) -> None:
    n = node(tmp_path)
    offer = {"type": "offer", "call_id": "c1", "sdp": "x"}
    n.track_call_signal(PEER, offer, event=ev(offer))
    n.backend.send_call_signal = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await n.decline_pending_call()
    sent = n.backend.send_call_signal.await_args.args
    assert sent[0] == PEER and sent[1]["type"] == "hangup" and sent[1]["call_id"] == "c1"
    assert n.pending_call_events() == []
