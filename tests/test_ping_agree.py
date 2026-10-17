"""Both phones show the same ping for their link; forged pongs are ignored."""

from __future__ import annotations

from typing import Any

from srltcp.core.protocol.messages import MessageType, build_header, encode_payload
from tests.test_transfer_engine import pair  # noqa: F401  (fixture)


async def test_both_phones_show_the_same_ping(pair: Any) -> None:  # noqa: F811
    """Alone, one phone measured 300 ms and the other 400 ms (at different
    moments, behind different traffic): with each other's figure, both show 350."""
    a, b = pair
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    la, lb = a.get_link(bh), b.get_link(ah)
    captured: list[tuple[Any, bytes]] = []

    async def capture(peer_id: str, transport: str, packet: bytes) -> None:
        captured.append((peer_id, packet))

    for node in (a, b):
        node._send_raw = capture  # type: ignore[method-assign]
    la.rtt_samples, lb.rtt_samples = [], []
    la.add_rtt(300.0)
    lb.add_rtt(400.0)
    await a.ping_peer(bh)  # carries a's 300
    await b.ping_peer(ah)  # carries b's 400
    pa, pb = (packet for _peer, packet in captured)
    peer_of = {x: next(p for p in x.tcp_transport.peers()
                       if p.peer_id == x.get_link(y.identities["tcp"].hash_id).transport_peer_id)
               for x, y in ((a, b), (b, a))}
    await b._on_transport_frame(peer_of[b], pa)
    await a._on_transport_frame(peer_of[a], pb)
    assert la.peer_rtt_ms == 400.0 and lb.peer_rtt_ms == 300.0
    la.add_rtt(300.0)
    lb.add_rtt(400.0)
    assert la.rtt_ms == lb.rtt_ms == 350.0


async def test_a_pong_answering_no_ping_of_ours_is_ignored(pair: Any) -> None:  # noqa: F811
    a, b = pair
    bh = b.identities["tcp"].hash_id
    link = a.get_link(bh)
    seen, rtt = link.last_seen, link.rtt_ms
    forged = build_header(MessageType.PONG, body=encode_payload({"sent_at": 1.0, "n": "00" * 8}))
    await a._on_transport_frame(
        next(p for p in a.tcp_transport.peers() if p.peer_id == link.transport_peer_id), forged)
    assert link.last_seen == seen and link.rtt_ms == rtt
