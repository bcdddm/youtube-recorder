"""MacWhisper Watch Folder adapter (P1 spike findings, 2026-07-19):

- SRT is exported to the SAME folder as the audio ({video_id}.srt next to
  {video_id}.m4a) — no separate outbox.
- Detection is near-instant; an 18-min video transcribes in ~2.5 min.
- Submit = atomic move (os.replace on same volume / shutil.move across).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SrtStatus:
    state: str  # missing | unstable | ready | timeout
    path: Path | None = None


def submit_audio(audio_path: Path, inbox: Path) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / audio_path.name
    if dest.exists() and dest.stat().st_size == audio_path.stat().st_size:
        return dest  # already submitted, idempotent
    tmp = inbox / (audio_path.name + ".part")
    shutil.copyfile(audio_path, tmp)
    tmp.replace(dest)  # atomic rename inside inbox volume
    return dest


def check_srt(video_id: str, inbox: Path, *, submitted_at: str | None,
              timeout_minutes: int, stable_seconds: float = 5.0) -> SrtStatus:
    srt = inbox / f"{video_id}.srt"
    if not srt.exists():
        if submitted_at and _minutes_since(submitted_at) > timeout_minutes:
            return SrtStatus("timeout")
        return SrtStatus("missing")
    st0 = srt.stat()
    if time.time() - st0.st_mtime < stable_seconds:
        size0 = st0.st_size
        time.sleep(stable_seconds)
        if srt.stat().st_size != size0:
            return SrtStatus("unstable")
    if srt.stat().st_size == 0:
        return SrtStatus("unstable")
    return SrtStatus("ready", path=srt)


def collect_srt(srt_path: Path, dest_dir: Path) -> Path:
    """Move the SRT into the per-video work dir as the immutable original."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "transcript.original.srt"
    shutil.move(str(srt_path), dest)
    return dest


def cleanup_inbox_audio(video_id: str, inbox: Path) -> None:
    audio = inbox / f"{video_id}.m4a"
    if audio.exists():
        audio.unlink()


def _minutes_since(iso: str) -> float:
    from datetime import datetime, timezone
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60
