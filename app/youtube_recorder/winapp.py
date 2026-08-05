"""Native window shell: Flask GUI wrapped in a WKWebView via pywebview.
No browser involved. Closing the window exits the app; the launchd worker
is a separate process and keeps running on schedule."""

from __future__ import annotations

import socket
import threading
import time

from . import BRANDING
import webbrowser as _wb
class _YtrApi:
    def open_external(self, url):
        try:
            _wb.open(url)
        except Exception:
            pass
        return True
_YTR_API = _YtrApi()

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

    _promote_to_regular_app()
    webview.create_window(BRANDING, f"http://{HOST}:{PORT}",
                          width=1120, height=820, min_size=(860, 600), js_api=_YTR_API)
    webview.start()  # blocks until window closed
    return 0


def _promote_to_regular_app() -> None:
    """本进程是打包后的 App 自己重新以 `app` 参数拉起的第二个实例（见
    tray.py 的 open_win()），主 Info.plist 是 LSUIElement=true（托盘常驻，
    不进 Dock）。这里把当前这一个实例的激活策略动态切回 Regular，让它
    单独在 Dock 显示——因为走的是打包好的同一个 App Bundle（而不是裸
    system python3 子进程），Dock 图标从进程一启动就是本 App 自己的图标，
    不会像以前那样先闪一下系统 Python 的火箭图标再切回来。"""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyRegular)
    except Exception:
        pass  # 拿不到 AppKit 也不影响窗口本身能不能用
