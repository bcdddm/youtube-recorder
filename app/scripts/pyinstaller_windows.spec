# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build of YouTube Recorder · By Leoluchino.

Sibling of pyinstaller.spec (the macOS one) — same collect_all() approach for
the same "these packages do dynamic/conditional imports and static scanning
misses them" packages, but with the macOS-only pieces stripped out:
  - no rumps/AppKit/Foundation/objc/Quartz/WebKit (tray + AppKit bits —
    tray.py is a separate opt-in subcommand not wired into the default
    Windows launch path yet; winapp.py's AppKit call is already wrapped in
    try/except so it no-ops cleanly without the module being importable)
  - no BUNDLE() step — that's a macOS .app-bundle concept (Info.plist,
    LSUIElement, etc.); Windows gets a plain onedir folder, portable/no
    installer for this first pass
  - .ico icon instead of .icns, no codesign step (that's macOS Gatekeeper
    ad-hoc signing, meaningless on Windows)

Output: dist/YouTube Recorder/YouTube Recorder.exe (onedir, portable — unzip
and run, nothing to install). Driven by scripts/build_app_windows.ps1, but
works fine standalone too:
    cd app && pyinstaller scripts/pyinstaller_windows.spec --noconfirm
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

# 跟 macOS 那份 spec 用同一份包清单，去掉 rumps（macOS 专属托盘，Windows
# 版这一步先不做——见模块顶部说明）。
for pkg in ("yt_dlp", "certifi", "anthropic", "openai", "pydantic",
           "pydantic_core", "webview", "markdown", "httpx",
           "httpcore", "h11", "sniffio", "anyio", "distro", "jiter", "tqdm",
           # 公司档案插件的点位图：yfinance 拉历史价格，靠这几个撑着
           "yfinance", "pandas", "numpy", "peewee", "multitasking",
           "frozendict", "platformdirs", "curl_cffi", "websockets",
           # API 密钥读写：keyring 在 Windows 上走 Credential Manager 后端，
           # 跟 macOS 上走 Keychain 后端一样是运行时按 entry points 选的
           "keyring"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "flask", "jinja2", "werkzeug", "markupsafe", "itsdangerous", "click",
    "blinker", "yaml", "PIL", "PIL.Image", "PIL.ImageFilter",
]

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
    # rumps/AppKit 一家子是 macOS 专属——即使某个依赖偷偷把它们拉进
    # collect_submodules 的扫描范围，也明确排除掉，Windows 上装不了这些包。
    excludes=["rumps", "AppKit", "Foundation", "objc", "objc._objc",
             "Quartz", "WebKit", "PyObjCTools", "PyObjC"],
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
    icon=str(ROOT / "scripts" / "AppIcon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YouTube Recorder",
)
