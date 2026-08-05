# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for "YouTube Recorder.app" · By Leoluchino.

Bundles the interpreter and every dependency into the .app itself so macOS
never has to fall back to identifying the running process as belonging to
the system Python.framework — that fallback is what used to make the Dock
briefly flash the generic Python rocket icon before winapp.py's runtime
fix kicked in. With everything embedded, the process is "our app" from the
moment it launches.

Driven by scripts/build_app.sh — not meant to be run standalone, but it
works fine on its own too:
    cd app && pyinstaller scripts/pyinstaller.spec --noconfirm
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # .../YouTube Recorder/app
sys.path.insert(0, str(ROOT))
from youtube_recorder import __version__  # noqa: E402

datas: list = []
binaries: list = []
hiddenimports: list = []

# 这些包在这个项目里大量用到延迟/条件 import（yt-dlp 的抽取器插件系统、
# pydantic 的动态 codegen、pywebview/rumps 的平台后端选择……），静态扫描
# 经常漏掉，所以用 collect_all 把它们的子模块和数据文件整个收进来，宁可
# 包体积大一点也不要打包完在真机上才发现缺 import。
for pkg in ("yt_dlp", "certifi", "anthropic", "openai", "pydantic",
           "pydantic_core", "rumps", "webview", "markdown", "httpx",
           "httpcore", "h11", "sniffio", "anyio", "distro", "jiter", "tqdm"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "AppKit", "Foundation", "objc", "WebKit", "Quartz",
    "flask", "jinja2", "werkzeug", "markupsafe", "itsdangerous", "click",
    "blinker", "yaml", "PIL", "PIL.Image", "PIL.ImageFilter",
]

# cli.py 用 __import__("youtube_recorder.xxx", ...) 字符串形式懒加载
# tray/winapp/gui 这几个子命令模块——PyInstaller 的静态字节码扫描跟不到
# 这种运行时拼字符串的 import，索性把整个自家包都收进来，省得每加一个
# 新子命令又要在这里补一条。
hiddenimports += collect_submodules("youtube_recorder")

a = Analysis(
    [str(ROOT / "pyinstaller_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YouTube Recorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YouTube Recorder",
)
app = BUNDLE(
    coll,
    name="YouTube Recorder.app",
    icon=str(ROOT / "scripts" / "AppIcon.icns"),
    bundle_identifier="com.leoluchino.youtube-recorder.app",
    info_plist={
        "CFBundleName": "YouTube Recorder",
        "CFBundleDisplayName": "YouTube Recorder",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "By Leoluchino",
        "NSHighResolutionCapable": True,
    },
)
