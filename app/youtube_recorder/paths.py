"""Application directory layout. All mutable state lives under one
per-platform app-data directory — never inside the Obsidian vault:
  macOS  : ~/Library/Application Support/YouTube Recorder/
  Windows: %LOCALAPPDATA%\\YouTube Recorder\\   (falls back to ~/AppData/Local)
  Linux  : $XDG_DATA_HOME/YouTube Recorder/     (falls back to ~/.local/share)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_app_support() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "YouTube Recorder"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "YouTube Recorder"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "YouTube Recorder"


APP_SUPPORT = Path(os.environ.get("YTREC_HOME", _default_app_support()))

CONFIG_FILE = APP_SUPPORT / "config.yaml"
DB_FILE = APP_SUPPORT / "state.sqlite3"
LOG_DIR = APP_SUPPORT / "logs"
WORK_DIR = APP_SUPPORT / "work"
DEAD_LETTER_DIR = APP_SUPPORT / "dead-letter"
LOCK_FILE = APP_SUPPORT / "ytrec.lock"


def ensure_dirs() -> None:
    for d in (APP_SUPPORT, LOG_DIR, WORK_DIR, DEAD_LETTER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_SUPPORT, 0o700)
    except OSError:
        pass  # Windows' chmod only toggles the read-only bit; nothing to do


def py_cmd() -> list[str]:
    """Python 调用前缀：macOS 上强制 arm64，避免 x86_64 父进程（Rosetta）
    污染架构偏好导致 arm64 原生扩展 dlopen 失败（0.2.9 修复）。"""
    if sys.platform == "darwin":
        return ["/usr/bin/arch", "-arm64", sys.executable]
    return [sys.executable]


def _installed_frozen_exe() -> Path | None:
    """已知的"已安装打包版"路径——只有明确知道固定安装位置的平台才填。
    macOS 装在 /Applications 下是这个项目自己的打包/部署脚本定的规矩；
    Windows 版目前还是免安装的 onedir 压缩包，没有固定路径，交给
    cli_launch_argv() 的开发环境兜底（裸解释器 + 源码目录）去处理。"""
    if sys.platform == "darwin":
        return Path("/Applications/YouTube Recorder.app/Contents/MacOS/YouTube Recorder")
    return None


def cli_launch_argv(*args: str) -> tuple[list[str], str | None]:
    """构造后台无窗口子命令（如 run --once --headless）的启动参数。

    当前进程本身就是打包后的可执行文件时，直接复用 sys.executable——
    这一定是对的，不用管到底装在哪（比以前写死 /Applications 路径更稳，
    也天然对 Windows 成立，因为 PyInstaller 在两个平台上都会把它设成
    真正在跑的那个可执行文件路径）。当前是源码环境跑（比如托盘/GUI
    还没打包过），就去找一个已知的"已安装打包版"顶替（目前只有 macOS
    有这个既定路径）；再找不到就退回裸解释器 + 源码目录。
    返回 (argv, cwd)，cwd 为 None 表示不需要显式指定工作目录。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args], None
    frozen = _installed_frozen_exe()
    if frozen is not None and frozen.exists():
        return [str(frozen), *args], None
    dev_root = Path(__file__).resolve().parents[1]
    return py_cmd() + ["-m", "youtube_recorder.cli", *args], str(dev_root)


def work_dir(video_id: str) -> Path:
    """Per-video working directory (metadata, audio, transcripts, frames...)."""
    d = WORK_DIR / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d
