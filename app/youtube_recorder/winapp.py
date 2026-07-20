"""Native window shell: Flask GUI wrapped in a WKWebView via pywebview.
No browser involved. Closing the window exits the app; the launchd worker
is a separate process and keeps running on schedule."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

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

    _set_dock_icon()
    webview.create_window(BRANDING, f"http://{HOST}:{PORT}",
                          width=1120, height=820, min_size=(860, 600))
    webview.start()  # blocks until window closed
    return 0


def _set_dock_icon() -> None:
    """裸 Python 进程在 Dock 会显示 Python 火箭图标——运行时替换为本应用图标。"""
    candidates = [
        "/Applications/YouTube Recorder.app/Contents/Resources/AppIcon.icns",
        str(Path(__file__).resolve().parents[2]
            / "YouTube Recorder.app/Contents/Resources/AppIcon.icns"),
    ]
    try:
        from AppKit import NSApplication, NSImage  # pyobjc（随 pywebview 安装）
        for p in candidates:
            if Path(p).exists():
                img = NSImage.alloc().initWithContentsOfFile_(p)
                if img:
                    NSApplication.sharedApplication().setApplicationIconImage_(img)
                return
    except Exception:
        pass  # 图标失败不影响功能
