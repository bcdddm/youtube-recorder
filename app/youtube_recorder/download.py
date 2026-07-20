"""Audio download (v0.2 §6.3 Downloader).

- Format 140 (native m4a) — no ffmpeg needed.
- Downloads into the per-video work dir (yt-dlp handles .part + rename).
- Disk headroom check before starting.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import work_dir
from .probe import _classify_error

MIN_FREE_BYTES = 2 * 1024**3  # 2 GB headroom


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    error_kind: str | None = None  # transient|permanent|resource
    reason: str = ""


def download_audio(video_id: str) -> DownloadResult:
    try:
        import yt_dlp
    except ImportError:
        return DownloadResult(False, error_kind="resource", reason="yt_dlp_not_installed")

    wd = work_dir(video_id)
    if shutil.disk_usage(wd).free < MIN_FREE_BYTES:
        return DownloadResult(False, error_kind="resource", reason="disk_low")

    target = wd / f"{video_id}.m4a"
    if target.exists() and target.stat().st_size > 0:
        return DownloadResult(True, path=target)  # idempotent

    opts = {
        "quiet": True, "no_warnings": True,
        "format": "140/bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(wd / f"{video_id}.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([f"https://www.youtube.com/watch?v={video_id}"])
    except yt_dlp.utils.DownloadError as e:
        return DownloadResult(False, error_kind=_classify_error(str(e)),
                              reason=str(e)[:200])

    candidates = sorted(wd.glob(f"{video_id}.m4a")) or sorted(wd.glob(f"{video_id}.*"))
    audio = next((p for p in candidates if p.suffix != ".part"), None)
    if audio is None or audio.stat().st_size == 0:
        return DownloadResult(False, error_kind="data", reason="empty_download")
    return DownloadResult(True, path=audio)
