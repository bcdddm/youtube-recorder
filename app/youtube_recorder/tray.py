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
            # 用 `open -n --args app` 重新拉起同一个打包好的 App Bundle 开
            # 一个窗口实例——PyInstaller 把解释器整个打进 App 里，不再依赖
            # 系统 python3。以前用裸 subprocess 跑系统 python3 时，系统的
            # framework 版 Python 一碰 AppKit/Cocoa 就会被 macOS 自动认领成
            # Python.framework 自己的 Resources/Python.app，Dock 图标会先闪
            # 一下系统 Python 的火箭图标再换回来；现在整个进程从启动那一刻
            # 就属于本 App Bundle，不会有这个中间态。见 winapp.py 的
            # _promote_to_regular_app()（把这一个实例的 Dock 激活策略切成
            # Regular，跟托盘那个 LSUIElement 常驻实例区分开）。
            win_app = "/Applications/YouTube Recorder.app"
            if Path(win_app).exists():
                subprocess.Popen(["open", "-n", win_app, "--args", "app"],
                                 start_new_session=True)
                return
            # 开发环境兜底（没打包过、直接跑源码调试用）：退回裸进程，图标可能会闪一下
            from .paths import py_cmd
            subprocess.Popen(
                py_cmd() + ["-m", "youtube_recorder.cli", "app"],
                cwd=str(APP_DIR), start_new_session=True)

        def run_now(self, _):
            # 无窗口/无 AppKit 的后台任务，不用像 open_win 那样过一遍 `open`
            # （那样反而拿不到子进程的 stdout/stderr 了）——直接跑打包好的
            # App 里的解释器本体（或开发环境兜底），日志照常能重定向进文件。
            from .paths import APP_SUPPORT, cli_launch_argv
            argv, cwd = cli_launch_argv("run", "--once", "--headless")
            subprocess.Popen(
                argv, cwd=cwd,
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
            # 匹配打包后窗口实例的进程命令行（.../MacOS/YouTube Recorder app）；
            # 开发环境兜底那条裸 python3 路径也顺带匹配到。
            subprocess.run(["pkill", "-f", "MacOS/YouTube Recorder app"],
                           capture_output=True)
            subprocess.run(["pkill", "-f", "youtube_recorder.cli app"],
                           capture_output=True)
            rumps.quit_application()

    Tray().run()
    return 0
