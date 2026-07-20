"""菜单栏托盘常驻（rumps）。

架构：托盘进程 = 常驻主进程，内嵌 Flask 服务；
"打开界面"时另起窗口子进程（pywebview），关窗只退窗口进程，托盘和服务照常；
菜单栏实时显示处理状态；退出从菜单走。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    import rumps

    from . import __version__
    from .winapp import HOST, PORT, _port_open

    if not _port_open():
        from . import gui
        threading.Thread(
            target=lambda: gui.app.run(host=HOST, port=PORT, debug=False,
                                       use_reloader=False),
            daemon=True).start()

    class Tray(rumps.App):
        def __init__(self):
            super().__init__("YouTube Recorder", title="▶︎", quit_button=None)
            self.status_item = rumps.MenuItem("状态加载中…")
            self.menu = [
                rumps.MenuItem("打开 YouTube Recorder", callback=self.open_win),
                rumps.MenuItem("⟳ 立即运行一轮", callback=self.run_now),
                None,
                self.status_item,
                rumps.MenuItem(f"v{__version__} · By Leoluchino"),
                None,
                rumps.MenuItem("退出", callback=self.quit_all),
            ]
            self._timer = rumps.Timer(self.refresh, 30)
            self._timer.start()
            self.refresh(None)

        def open_win(self, _):
            from .paths import py_cmd
            subprocess.Popen(
                py_cmd() + ["-m", "youtube_recorder.cli", "app"],
                cwd=str(APP_DIR), start_new_session=True)

        def run_now(self, _):
            from .paths import APP_SUPPORT
            from .paths import py_cmd
            subprocess.Popen(
                py_cmd() + ["-m", "youtube_recorder.cli", "run",
                 "--once", "--headless"],
                cwd=str(APP_DIR),
                stdout=open(APP_SUPPORT / "logs" / "manual-run.log", "ab"),
                stderr=subprocess.STDOUT, start_new_session=True)
            self.status_item.title = "已触发运行…"

        def refresh(self, _):
            try:
                from . import db as dbm
                con = dbm.connect()
                c = dbm.counts_by_status(con)
                con.close()
                done = c.get("verified", 0)
                fail = c.get("failed", 0) + c.get("dead_letter", 0)
                active = sum(v for k, v in c.items()
                             if k not in ("verified", "ignored",
                                          "failed", "dead_letter"))
                parts = [f"✓ {done}"]
                if active:
                    parts.append(f"⟳ {active}")
                if fail:
                    parts.append(f"✗ {fail}")
                self.status_item.title = "状态： " + " · ".join(parts)
                self.title = "▶︎" if not active else "◉"
            except Exception:
                self.status_item.title = "状态不可用"

        def quit_all(self, _):
            subprocess.run(["pkill", "-f", "youtube_recorder.cli app"],
                           capture_output=True)
            rumps.quit_application()

    Tray().run()
    return 0
