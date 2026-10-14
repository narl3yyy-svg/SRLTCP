"""Encryption at rest (vault) and encrypted backup / restore."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from srltcp.core import backup
from srltcp.utils import vault
from srltcp.utils.jsonstore import load_json, save_json
from tests.test_e2e_nodes import make_node, trust, wait_for


def test_files_are_sealed_and_bound_to_their_name(tmp_path: Path) -> None:
    path = tmp_path / "trusted.json"
    save_json(path, {"peers": [{"name": "Alice"}]})
    raw = path.read_bytes()
    assert raw.startswith(vault.MAGIC) and b"Alice" not in raw
    assert load_json(path) == {"peers": [{"name": "Alice"}]}
    assert (tmp_path / "datakey").stat().st_mode & 0o777 == 0o600
    # Swapping one sealed file for another is detected (name is authenticated).
    other = tmp_path / "messages.json"
    other.write_bytes(raw)
    with pytest.raises(vault.VaultError):
        vault.read_text(other)


def test_plain_files_from_older_versions_are_read_then_sealed(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"display_name": "old"}))
    assert load_json(path) == {"display_name": "old"}
    assert vault.seal_existing([path, tmp_path / "missing.json"]) == 1
    assert vault.is_sealed(path) and load_json(path) == {"display_name": "old"}
    assert vault.seal_existing([path]) == 0  # already sealed


def test_unreadable_file_is_moved_aside_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "trusted.json"
    save_json(path, {"peers": []})
    sealed = path.read_bytes()
    (tmp_path / "datakey").write_bytes(b"\x01" * 32)  # another device's key
    vault.forget_keys()
    assert load_json(path, "default") == "default"
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("trusted.json.unreadable-")]
    assert len(aside) == 1 and aside[0].read_bytes() == sealed
    vault.forget_keys()


async def test_node_keeps_everything_encrypted(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        bh = b.identities["tcp"].hash_id
        await a.send_message(bh, "secret words")
        assert await wait_for(lambda: any(m["text"] == "secret words" for m in b.get_messages()))
        a.save_message_history()
        b.save_message_history()
        for node in ("alice", "bob"):
            folder = tmp_path / node
            for name in ("trusted.json", "messages.json", "identities/identity_tcp.json"):
                raw = (folder / name).read_bytes()
                assert raw.startswith(vault.MAGIC), name
                assert b"secret words" not in raw and b"private_key" not in raw
    finally:
        await a.stop()
        await b.stop()


async def test_backup_restores_identity_contacts_and_chats(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.send_message(bh, "before the backup")
        assert await wait_for(
            lambda: any(m["text"] == "before the backup" for m in b.get_messages())
        )
        a.save_message_history()
        root = tmp_path / "alice"
        save_json(root / "settings.json", {"display_name": "alice", "incoming_files_dir": "/x"})
        blob = backup.create_backup(root, "correct horse battery")
    finally:
        await a.stop()
        await b.stop()
    assert b"before the backup" not in blob and b"alice" not in blob

    with pytest.raises(backup.BackupError, match="wrong password"):
        backup.open_backup(blob, "wrong password!")
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(backup.BackupError):
        backup.open_backup(tampered, "correct horse battery")
    with pytest.raises(backup.BackupError, match="not an SRLTCP backup"):
        backup.open_backup(b"PK\x03\x04zip", "correct horse battery")
    with pytest.raises(backup.BackupError, match="at least"):
        backup.create_backup(root, "short")

    data = backup.open_backup(blob, "correct horse battery")
    info = backup.summary(data)
    assert info["hash_id"] == ah and info["contacts"] == 1 and info["messages"] >= 1

    # A new device: another data folder (and data key) entirely.
    newroot = tmp_path / "newphone"
    save_json(newroot / "transfers.json", {"transfers": [{"id": "stale"}]})
    backup.restore_backup(newroot, data)
    assert not (newroot / "transfers.json").exists()
    settings = load_json(newroot / "settings.json")
    assert "incoming_files_dir" not in settings and settings["setup_complete"] is True
    restored = make_node(tmp_path, "newphone")
    await restored.start()
    try:
        assert restored.identities["tcp"].hash_id == ah
        assert restored.is_trusted(bh)
        assert any(m["text"] == "before the backup" for m in restored.get_messages())
    finally:
        await restored.stop()


async def test_backup_and_restore_over_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import srltcp.web.server  # noqa: F401 - the app's import order (routes import web)
    from srltcp.core.messaging.backend import NodeConfig
    from srltcp.core.node import SRLTCPNode
    from srltcp.core.settings import AppSettings
    from srltcp.routes import api

    restarts: list[list[str]] = []
    monkeypatch.setattr(api.os, "execv", lambda exe, argv: restarts.append(argv))

    def make(name: str) -> SRLTCPNode:
        monkeypatch.setenv("SRLTCP_DATA_DIR", str(tmp_path / name))
        cfg = NodeConfig(name=name, bind_host="127.0.0.1", tcp_port=0, discovery_port=0,
                         incoming_dir=str(tmp_path / name / "incoming"))
        return SRLTCPNode(cfg, AppSettings(setup_complete=True, display_name=name))

    old = make("old")
    await old.start()
    old_hash = old.backend.identities["tcp"].hash_id
    app = web.Application()
    api.register_api_routes(app, old)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/backup", json={"password": "short"})
        assert resp.status == 400
        resp = await client.post("/api/backup", json={"password": "a long passphrase"})
        name = (await resp.json())["filename"]
        resp = await client.get(f"/api/backup/{name}")
        assert resp.status == 200 and "attachment" in resp.headers["Content-Disposition"]
        blob = await resp.read()
        assert (await client.get("/api/backup/..%2Fsettings.json")).status == 404
    await old.stop()

    fresh = make("fresh")
    await fresh.start()
    assert fresh.backend.identities["tcp"].hash_id != old_hash
    app = web.Application()
    api.register_api_routes(app, fresh)
    async with TestClient(TestServer(app)) as client:
        form = aiohttp.FormData()
        form.add_field("password", "not the password")
        form.add_field("file", blob, filename=name)
        resp = await client.post("/api/backup/restore", data=form)
        assert resp.status == 400 and "wrong password" in (await resp.json())["error"]
        assert fresh.backend._running  # nothing was touched

        form = aiohttp.FormData()
        form.add_field("password", "a long passphrase")
        form.add_field("file", blob, filename=name)
        resp = await client.post("/api/backup/restore", data=form)
        body = await resp.json()
        assert resp.status == 200 and body["restored"] and body["hash_id"] == old_hash
        assert not fresh.backend._running  # stopped before the files were replaced
        await asyncio.sleep(0.7)
        assert restarts  # the desktop app restarts itself
    again = make("fresh")
    await again.start()
    try:
        assert again.backend.identities["tcp"].hash_id == old_hash
    finally:
        await again.stop()
