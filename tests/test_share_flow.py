"""Sharing end to end: offline recipients, passwords, lockout, restarts."""

from __future__ import annotations

import asyncio
from pathlib import Path

from srltcp.utils import vault
from tests.test_e2e_nodes import make_node, trust, wait_for


async def _pair(tmp_path: Path):  # type: ignore[no-untyped-def]
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    return a, b


def _folder(tmp_path: Path) -> Path:
    shared = tmp_path / "to-share"
    (shared / "sub").mkdir(parents=True, exist_ok=True)
    (shared / "a.txt").write_text("hello")
    (shared / "sub" / "b.txt").write_text("world")
    return shared


async def test_offer_waits_for_offline_contact_then_arrives(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        grants = await a.offer_share([bh], folder=_folder(tmp_path), label="Docs")
        assert grants and grants[0]["waiting"] is True
        await a.connect_to_peer(bh)
        offered = lambda: any(g["owner_hash"] == ah for g in b.list_remote_share_grants())  # noqa: E731
        assert await wait_for(offered)
        assert not a.list_local_share_grants()[0]["waiting"]
        gid = grants[0]["grant_id"]
        entries = await b.request_share_list(ah, gid, timeout=5)
        names = sorted(e["name"] for e in entries or [] if e["type"] == "file")
        assert names == ["a.txt", "sub/b.txt"]
    finally:
        await a.stop()
        await b.stop()


async def test_password_protected_share(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh)
        grants = await a.offer_share([bh], folder=_folder(tmp_path), password="s3cret")
        gid = grants[0]["grant_id"]
        assert await wait_for(lambda: b.list_remote_share_grants())
        assert b.list_remote_share_grants()[0]["password"] is True
        shares = tmp_path / "alice" / "shares.json"
        assert b"s3cret" not in shares.read_bytes()  # encrypted at rest…
        assert "s3cret" not in vault.read_text(shares)  # …and only a hash inside

        assert await b.request_share_list(ah, gid, timeout=5) == []
        assert b.share_list_error(gid) == "password_required"
        assert await b.request_share_list(ah, gid, timeout=5, password="nope") == []
        assert b.share_list_error(gid) == "wrong_password"
        entries = await b.request_share_list(ah, gid, timeout=5, password="s3cret")
        assert entries and b.share_list_error(gid) == ""
        # Downloads use the remembered password; a fetch with a wrong one is refused.
        assert await b.request_share_file(ah, gid, "a.txt")
        assert await wait_for(lambda: a.list_local_share_grants()[0]["download_count"] == 1)
    finally:
        await a.stop()
        await b.stop()


async def test_wrong_passwords_lock_the_share(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh)
        grants = await a.offer_share([bh], folder=_folder(tmp_path), password="right")
        gid = grants[0]["grant_id"]
        assert await wait_for(lambda: b.list_remote_share_grants())
        for i in range(5):
            await b.request_share_list(ah, gid, timeout=5, password=f"guess{i}")
        assert await b.request_share_list(ah, gid, timeout=5, password="right") == []
        assert b.share_list_error(gid) == "locked"
    finally:
        await a.stop()
        await b.stop()


async def test_shares_survive_restart_and_stranger_is_refused(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path)
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    grants = await a.offer_share([bh], folder=_folder(tmp_path), ttl_preset="1w")
    gid = grants[0]["grant_id"]
    await a.stop()
    await b.stop()
    a2 = make_node(tmp_path, "alice")
    b2 = make_node(tmp_path, "bob")
    await a2.start()
    await b2.start()
    try:
        assert [g["grant_id"] for g in a2.list_local_share_grants()] == [gid]
        b2.trusted.update(ah, tcp_host="127.0.0.1", tcp_port=a2.config.tcp_port)
        await b2.connect_to_peer(ah)
        assert await wait_for(lambda: b2.list_remote_share_grants(), timeout=8)
        # Untrusted recipients are skipped; nothing is shared with them.
        assert await a2.offer_share(["ef" * 16], folder=_folder(tmp_path)) == []
        await asyncio.sleep(0)
    finally:
        await a2.stop()
        await b2.stop()
