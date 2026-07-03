"""Android foreground service entry — runs the SRLTCP web/P2P node."""

from __future__ import annotations

import os
import sys


def _configure_android_paths() -> None:
    os.environ.setdefault("SRLTCP_ANDROID", "1")
    try:
        from jnius import autoclass

        PythonService = autoclass("org.kivy.android.PythonService")
        context = PythonService.mService.getApplicationContext()
        files_dir = context.getFilesDir().getAbsolutePath()
        from srltcp.utils.platform import set_android_data_dir

        set_android_data_dir(files_dir)
    except Exception:
        pass


def main() -> None:
    _configure_android_paths()
    sys.argv = ["srltcp", "web", "--log-level", "INFO"]
    from srltcp.app import main as srltcp_main

    srltcp_main()


if __name__ == "__main__":
    main()