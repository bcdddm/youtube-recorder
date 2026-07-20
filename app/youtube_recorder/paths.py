"""Application directory layout. All mutable state lives under
~/Library/Application Support/YouTube Recorder/ — never inside the Obsidian vault."""

from __future__ import annotations

import os
from pathlib import Path

APP_SUPPORT = Path(
    os.environ.get(
        "YTREC_HOME",
        Path.home() / "Library" / "Application Support" / "YouTube Recorder",
    )
)

CONFIG_FILE = APP_SUPPORT / "config.yaml"
DB_FILE = APP_SUPPORT / "state.sqlite3"
LOG_DIR = APP_SUPPORT / "logs"
WORK_DIR = APP_SUPPORT / "work"
DEAD_LETTER_DIR = APP_SUPPORT / "dead-letter"
LOCK_FILE = APP_SUPPORT / "ytrec.lock"


def ensure_dirs() -> None:
    for d in (APP_SUPPORT, LOG_DIR, WORK_DIR, DEAD_LETTER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(APP_SUPPORT, 0o700)


def py_cmd() -> list[str]:
    """Python 调用前缀：macOS 上强制 arm64，避免 x86_64 父进程（Rosetta）
    污染架构偏好导致 arm64 原生扩展 dlopen 失败（0.2.9 修复）。"""
    import sys
    if sys.platform == "darwin":
        return ["/usr/bin/arch", "-arm64", sys.executable]
    return [sys.executable]


def work_dir(video_id: str) -> Path:
    """Per-video working directory (metadata, audio, transcripts, frames...)."""
    d = WORK_DIR / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d
