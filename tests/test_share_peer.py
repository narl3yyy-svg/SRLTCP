"""E2EE shared folder access control tests."""

from __future__ import annotations

import time
from pathlib import Path

from srltcp.core.messaging.share_peer import (
    ShareGrant,
    resolve_download_limit,
    resolve_share_ttl,
)


def test_share_grant_valid_for_recipient() -> None:
    recipient = "a" * 32
    grant = ShareGrant(
        grant_id="abc123",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp/shared"),
        expires=time.time() + 3600,
    )
    assert grant.valid_for(recipient)
    assert not grant.valid_for("c" * 64)


def test_share_grant_expired() -> None:
    recipient = "a" * 32
    grant = ShareGrant(
        grant_id="expired",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp/shared"),
        expires=time.time() - 10,
    )
    assert not grant.valid_for(recipient)


def test_share_grant_download_limit() -> None:
    recipient = "a" * 32
    grant = ShareGrant(
        grant_id="g1",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp"),
        expires=time.time() + 100,
        max_downloads=2,
    )
    assert grant.record_download()
    assert grant.record_download()
    assert not grant.record_download()
    assert not grant.valid_for(recipient)


def test_share_grant_revoked() -> None:
    recipient = "a" * 32
    grant = ShareGrant(
        grant_id="g2",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp"),
        expires=time.time() + 100,
        revoked=True,
    )
    assert not grant.valid_for(recipient)


def test_resolve_share_ttl_presets() -> None:
    assert resolve_share_ttl("1m") > time.time()
    assert resolve_share_ttl("forever") > time.time() + 365 * 86400


def test_resolve_download_limit() -> None:
    assert resolve_download_limit("unlimited") == 0
    assert resolve_download_limit("5") == 5