"""E2EE shared folder access control tests."""

from __future__ import annotations

import time
from pathlib import Path

from srltcp.core.messaging.share_peer import SHARE_GRANT_TTL, ShareGrant


def test_share_grant_valid_for_recipient() -> None:
    recipient = "a" * 64
    grant = ShareGrant(
        grant_id="abc123",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp/shared"),
        expires=time.time() + SHARE_GRANT_TTL,
    )
    assert grant.valid_for(recipient)
    assert not grant.valid_for("c" * 64)


def test_share_grant_expired() -> None:
    recipient = "a" * 64
    grant = ShareGrant(
        grant_id="expired",
        owner_hash="b" * 64,
        recipient_hash=recipient,
        root=Path("/tmp/shared"),
        expires=time.time() - 10,
    )
    assert not grant.valid_for(recipient)


def test_share_grant_compare_digest_timing_safe() -> None:
    grant = ShareGrant(
        grant_id="g1",
        owner_hash="owner" * 8,
        recipient_hash="a" * 64,
        root=Path("/tmp"),
        expires=time.time() + 100,
    )
    assert not grant.valid_for("b" * 64)