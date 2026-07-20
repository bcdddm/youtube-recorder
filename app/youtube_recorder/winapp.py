"""Native window shell: Flask GUI wrapped in a WKWebView via pywebview.
No browser involved. Closing the window exits the app; the launchd worker
is a separate process and keeps running on schedule."""

from __future__ import annotations

import socket
import threading
import time

from . import BRANDING

HOST, PORT = "127.0.0.1", 8765


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def main() -> int:
    import webview  # pywebview

    if not _port_open():
        from . import gui
        t = threading.Thread(
            target=lambda: gui.app.run(host=HOST, port=PORT, debug=False,
                                       use_reloader=False),
            daemon=True)
        t.start()
        for _ in range(40):
            if _port_open():
                break
            time.sleep(0.25)

    webview.create_window(BRANDING, f"http://{HOST}:{PORT}",
                          width=1120, height=820, min_size=(860, 600))
    webview.start()  # blocks until window closed
    return 0
