"""Post Android notifications straight from Python.

The web UI posts notifications while its WebView is alive; once the user
swipes the app away only the Python node (kept alive by the foreground
service) is left, so it must notify by itself.
"""

from __future__ import annotations

from srltcp.utils.logging import get_logger
from srltcp.utils.platform import is_android

log = get_logger(__name__)


def post_android_notification(title: str, body: str, tag: str) -> bool:
    if not is_android():
        return False
    try:
        from java import jclass

        app = jclass("com.srltcp.app.SRLTCPApplication").getInstance()
        if app is None:
            return False
        notifier = jclass("com.srltcp.app.SRLTCPNotifier").INSTANCE
        notifier.postAlert(app, title or "SRLTCP", body or "", tag or "")
        return True
    except Exception as exc:  # noqa: BLE001 - notifications are best effort
        log.debug("Android notification failed: %s", exc)
        return False


def _notifier():  # type: ignore[no-untyped-def]
    from java import jclass

    app = jclass("com.srltcp.app.SRLTCPApplication").getInstance()
    return app, jclass("com.srltcp.app.SRLTCPNotifier").INSTANCE


def ring_android_call(caller: str, video: bool) -> bool:
    """Ring like a phone call (the phone's ringtone, over the lock screen)."""
    if not is_android():
        return False
    try:
        app, notifier = _notifier()
        if app is None:
            return False
        notifier.ringIncomingCall(app, caller or "SRLTCP", bool(video))
        return True
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("Android call ring failed: %s", exc)
        return False


def stop_android_ringing() -> None:
    if not is_android():
        return
    try:
        app, notifier = _notifier()
        if app is not None:
            notifier.stopRinging(app)
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("Android stop ringing failed: %s", exc)
