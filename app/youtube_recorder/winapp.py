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

# 起窗口最花时间的一段是 webview.start() 内部——AppKit/PyObjC 桥接 +
# WebKit 引擎冷启动，这段跟咱自己的 Flask/DB 代码无关（实测：加载真实
# 页面本身 200ms 内就完事），没法从代码层面缩短。真正能改善的是"这段时间
# 窗口里显示什么"：以前是直接指向 http://127.0.0.1:8765/，WebKit 引擎
# 起来之前窗口里什么都没有；现在先用内嵌的本地 HTML（不用等网络/Flask）
# 弹出一个跟正式界面同色系的加载态，WebKit 引擎一就绪就能画出来，然后
# 在后台把真正页面 swap 进来，不用再盯着一片空白等。
_LOADING_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{height:100%;margin:0;background:#121215;color:#e9e9ec;
    font:14.5px/1.65 -apple-system,"PingFang SC",sans-serif;
    display:flex;align-items:center;justify-content:center;
    -webkit-user-select:none;user-select:none;cursor:default}}
  .wrap{{display:flex;flex-direction:column;align-items:center;gap:14px}}
  .ring{{width:38px;height:38px;border-radius:50%;
    border:3px solid rgba(255,255,255,.12);border-top-color:#e0a458;
    animation:spin .9s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
  .t{{color:#9a9aa3;font-size:13px}}
</style></head>
<body><div class="wrap"><div class="ring"></div>
<div class="t">{BRANDING} 正在启动…</div></div></body></html>"""


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def main() -> int:
    import webview  # pywebview

    port_ready = _port_open()
    if not port_ready:
        from . import gui
        t = threading.Thread(
            target=lambda: gui.app.run(host=HOST, port=PORT, debug=False,
                                       use_reloader=False),
            daemon=True)
        t.start()

    _promote_to_regular_app()
    window = webview.create_window(BRANDING, html=_LOADING_HTML,
                                   width=1120, height=820, min_size=(860, 600),
                                   js_api=_YTR_API, background_color="#121215")

    def _go_to_real_ui():
        # pywebview 保证这个函数在 GUI 事件循环真正跑起来之后、另开一条
        # 线程执行——窗口这时已经带着上面的加载态显示出来了，这里只是
        # 确认 Flask 端口就绪（一般早就绪了，tray 那边常驻的服务器多半
        # 已经在跑）然后把地址切到真正的页面。
        if not port_ready:
            for _ in range(40):
                if _port_open():
                    break
                time.sleep(0.25)
        window.load_url(f"http://{HOST}:{PORT}")

    webview.start(_go_to_real_ui)  # blocks until window closed
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
