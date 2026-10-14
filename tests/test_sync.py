"""Folder sync: two-way, conflicts, deletes to trash, and what a contact can't do."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from srltcp.core.messaging.sync import META_DIR, _vv_cmp, clean_rel
from srltcp.core.protocol.messages import encode_payload
from tests.test_e2e_nodes import make_node, trust, wait_for


def files_in(root: Path) -> dict[str, bytes]:
    out = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith(META_DIR):
            out[rel] = path.read_bytes()
    return out


def test_contact_paths_are_checked() -> None:
    assert clean_rel("a/b.txt") == "a/b.txt"
    for bad in ("../x", "a/../../x", "/etc/passwd", "C:/x", "a\\b", "", ".", "a//b/../c",
                f"{META_DIR}/trash/x", "a/ b", "x\x00y", "a" * 2000, 5, None):
        assert clean_rel(bad) is None, bad


def test_version_vectors() -> None:
    assert _vv_cmp({"a": 2}, {"a": 1}) == 1
    assert _vv_cmp({"a": 1}, {"a": 1, "b": 1}) == -1
    assert _vv_cmp({"a": 2, "b": 1}, {"a": 1, "b": 2}) == 2
    assert _vv_cmp({}, {}) == 0


@pytest.fixture
async def synced(tmp_path: Path):  # type: ignore[no-untyped-def]
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    await a.connect_to_peer(bh)
    assert await a.wait_for_handshake(bh, timeout=8)
    root_a, root_b = tmp_path / "A", tmp_path / "B"
    (root_a / "docs").mkdir(parents=True)
    (root_a / "hello.txt").write_text("hello\n")
    (root_a / "docs" / "big.bin").write_bytes(os.urandom(300_000))
    sync = await a.create_sync(bh, root_a, "Shared")
    sid = sync["id"]
    assert await wait_for(lambda: sid in b._syncs and b._syncs[sid].state == "offered")
    assert b.list_syncs(ah)[0]["name"] == "Shared"
    await b.accept_sync(sid, root_b)
    assert await wait_for(lambda: files_in(root_b) == files_in(root_a), timeout=20)
    yield a, b, sid, root_a, root_b
    await a.stop()
    await b.stop()


async def test_changes_and_deletes_flow_both_ways(synced) -> None:  # type: ignore[no-untyped-def]
    a, b, sid, root_a, root_b = synced
    assert a._syncs[sid].state == "active" and b._syncs[sid].state == "active"
    (root_a / "hello.txt").write_text("hello again\n")
    (root_a / "new" / "deep").mkdir(parents=True)
    (root_a / "new" / "deep" / "n.txt").write_text("new file")
    await a.sync_now(sid)
    assert await wait_for(lambda: files_in(root_b) == files_in(root_a), timeout=20)
    # A delete on B: gone on A too, but kept in A's trash.
    (root_b / "docs" / "big.bin").unlink()
    await b.sync_now(sid)
    assert await wait_for(lambda: not (root_a / "docs" / "big.bin").exists(), timeout=20)
    assert list((root_a / META_DIR / "trash").rglob("big.bin"))
    assert not (root_a / "docs").exists()  # emptied folder removed
    # Edits from B come back to A.
    (root_b / "hello.txt").write_text("from bob\n")
    await b.sync_now(sid)
    assert await wait_for(lambda: (root_a / "hello.txt").read_text() == "from bob\n", timeout=20)
    # Nothing changed: a sync round transfers nothing.
    before = len(a._transfers) + len(b._transfers)
    await a.sync_now(sid)
    await b.sync_now(sid)
    assert len(a._transfers) + len(b._transfers) == before


async def test_conflicting_edits_keep_both_versions(synced) -> None:  # type: ignore[no-untyped-def]
    a, b, sid, root_a, root_b = synced
    (root_a / "hello.txt").write_text("alice's edit\n")
    (root_b / "hello.txt").write_text("bob's edit\n")
    await a._scan(a._syncs[sid])
    await b._scan(b._syncs[sid])
    await a.sync_now(sid)
    await b.sync_now(sid)

    def converged() -> bool:
        fa, fb = files_in(root_a), files_in(root_b)
        return fa == fb and len(fa) == 3

    for _ in range(4):
        if await wait_for(converged, timeout=6):
            break
        await a.sync_now(sid)
        await b.sync_now(sid)
    assert converged(), (sorted(files_in(root_a)), sorted(files_in(root_b)))
    contents = {v for k, v in files_in(root_a).items() if "hello" in k}
    assert contents == {b"alice's edit\n", b"bob's edit\n"}  # nothing lost
    assert any("conflict" in k for k in files_in(root_a))


async def test_a_contact_cannot_escape_the_folder_or_take_files(  # type: ignore[no-untyped-def]
    synced, tmp_path: Path
) -> None:
    a, b, sid, root_a, root_b = synced
    ah = a.identities["tcp"].hash_id
    evil = {"sha": "0" * 64, "size": 3, "mtime": 1, "vv": {ah: 99}, "del": False}
    tomb = {"sha": "", "size": 0, "mtime": 0, "vv": {ah: 99}, "del": True}
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    await b._merge_remote(b._syncs[sid], {
        "../outside.txt": tomb, "/etc/passwd": evil, f"{META_DIR}/x": evil, "a\\b": evil,
    })
    assert outside.read_text() == "keep me"
    assert not b._sync_queue.get(sid)
    # A file is only sent if it is in the index with exactly that hash.
    before = len(a._transfers)
    for path, sha in (("hello.txt", "1" * 64), ("../../etc/passwd", "1" * 64),
                      ("missing.txt", "2" * 64)):
        await a._handle_sync(
            b.identities["tcp"].hash_id,
            encode_payload({"op": "get", "id": sid, "path": path, "sha": sha}),
        )
    assert len(a._transfers) == before
    # An offer for a file B didn't ask for is refused.
    assert b.sync_offer_target(ah, {"id": sid, "path": "x.txt", "sha": "3" * 64}, 5) is None
    # Another contact can't touch this sync.
    c = make_node(tmp_path, "carol")
    c.identities["tcp"] = c.identity_store.load_or_create("carol", "tcp")
    await b._handle_sync(
        c.identities["tcp"].hash_id,
        encode_payload({"op": "index", "id": sid, "files": {"hello.txt": tomb}}),
    )
    assert (root_b / "hello.txt").exists()


async def test_stop_on_one_side_stops_the_other(synced) -> None:  # type: ignore[no-untyped-def]
    a, b, sid, root_a, root_b = synced
    assert await a.remove_sync(sid)
    assert await wait_for(lambda: b._syncs[sid].state == "stopped")
    assert files_in(root_b) and files_in(root_a)  # files stay
