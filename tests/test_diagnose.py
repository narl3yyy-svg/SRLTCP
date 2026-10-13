"""Check connection: explains which of a contact's addresses work."""

from __future__ import annotations

from pathlib import Path

from tests.test_e2e_nodes import make_node, trust


async def test_diagnose_reports_reachable_and_dead_addresses(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        bh = b.identities["tcp"].hash_id
        a.trusted.update(bh, tcp_host="127.0.0.1", tcp_port=b.config.tcp_port,
                         alt_hosts=["192.0.2.55"])
        report = await a.diagnose_contact(bh)
        by_addr = {r["address"].split(":")[0]: r for r in report["addresses"]}
        assert by_addr["127.0.0.1"]["ok"] is True
        assert by_addr["192.0.2.55"]["ok"] is False
        assert "directly" in report["summary"]

        a.trusted.update(bh, tcp_host="192.0.2.55", alt_hosts=[])
        report = await a.diagnose_contact(bh)
        assert "Can't reach" in report["summary"] and "AllowedIPs" in report["summary"]
        assert (await a.diagnose_contact("ab" * 16)) == {"error": "not a contact"}
    finally:
        await a.stop()
        await b.stop()
