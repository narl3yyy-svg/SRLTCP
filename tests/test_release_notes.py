"""The notes shown when tapping the version are those of this version."""

from __future__ import annotations

import re

import srltcp.web.server  # noqa: F401  (the app's import order: routes import web)
from srltcp import __version__
from srltcp.routes.api import RELEASE_NOTES_PATH


def test_release_notes_start_with_this_version() -> None:
    text = RELEASE_NOTES_PATH.read_text(encoding="utf-8")
    first = re.search(r"^## v(\S+)", text, re.MULTILINE)
    assert first is not None
    assert first.group(1) == __version__
