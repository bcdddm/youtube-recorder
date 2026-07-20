"""yt-dlp metadata probe + policy filter + caption fast path (v0.2 §6.3).

Decisions returned to the pipeline:
  ignore(reason)      — shorts / live / too short
  captions(lang,file) — manual captions downloaded (fast path)
  transcribe          — no usable captions, queue audio
  permanent(reason)   — private / deleted / members-only / geo-blocked
  transient(reason)   — network etc., retry later
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import work_dir

# preference order (audit: pick ONE track by explicit priority, keep its tag)
SUB_PRIORITY = ["zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh", "en", "en-US", "en-GB"]

PERMANENT_MARKERS = (
    "private video", "video unavailable", "members-only", "join this channel",
    "removed by the uploader", "account associated with this video has been terminated",
    "not available in your country", "age-restricted", "sign in to confirm your age",
)


@dataclass
class ProbeResult:
    action: str                 # ignore|captions|transcribe|permanent|transient
    reason: str = ""
    title: str = ""
    duration_sec: int = 0
    published_at: str | None = None
    caption_lang: str | None = None
    caption_file: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None


def _classify_error(msg: str) -> str:
    low = msg.lower()
    return "permanent" if any(m in low for m in PERMANENT_MARKERS) else "transient"


def probe(video_id: str, cfg) -> ProbeResult:
    try:
        import yt_dlp
    except ImportError:
        return ProbeResult("transient", reason="yt_dlp_not_installed")

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as y:
            info = y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        kind = _classify_error(str(e))
        return ProbeResult(kind, reason=str(e)[:200])

    title = info.get("title") or ""
    duration = int(info.get("duration") or 0)
    upload_date = info.get("upload_date")  # YYYYMMDD
    published = (f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"
                 if upload_date else None)

    base = dict(title=title, duration_sec=duration, published_at=published,
                channel_id=info.get("channel_id"),
                channel_name=info.get("channel") or info.get("uploader"))

    live = info.get("live_status")
    if info.get("is_live") or live in ("is_live", "is_upcoming", "post_live"):
        if not cfg.get("discovery.include_live", False):
            return ProbeResult("ignore", reason=f"live:{live}", **base)
    if duration and duration < cfg.get("discovery.min_duration_sec", 90):
        if not cfg.get("discovery.include_shorts", False):
            return ProbeResult("ignore", reason=f"short:{duration}s", **base)

    manual = info.get("subtitles") or {}
    lang = next((l for l in SUB_PRIORITY if l in manual), None)
    if lang is None:
        # any other manual track still beats transcription
        lang = next(iter(manual), None)
    if lang:
        f = _download_captions(video_id, url, lang)
        if f:
            return ProbeResult("captions", caption_lang=lang, caption_file=str(f), **base)
    return ProbeResult("transcribe", **base)


def _download_captions(video_id: str, url: str, lang: str) -> Path | None:
    import yt_dlp
    wd = work_dir(video_id)
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "writesubtitles": True, "subtitleslangs": [lang],
        "subtitlesformat": "vtt/srt/best",
        "outtmpl": str(wd / "captions.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    except yt_dlp.utils.DownloadError:
        return None
    for ext in ("vtt", "srt"):
        for p in wd.glob(f"captions*.{ext}"):
            return p
    return None
