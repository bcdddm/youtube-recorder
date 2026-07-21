'''Audio download. YouTube/Bilibili via yt-dlp; podcasts via direct HTTP GET.'''
from __future__ import annotations
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from .paths import work_dir
from .probe import _classify_error

MIN_FREE_BYTES = 2 * 1024**3


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    error_kind: str | None = None
    reason: str = ''


def _ssl_ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_download(url, target):
    req = urllib.request.Request(url, headers={'User-Agent': 'YouTubeRecorder/1.0 (personal use)'})
    with urllib.request.urlopen(req, timeout=180, context=_ssl_ctx()) as resp:
        with open(target, 'wb') as f:
            shutil.copyfileobj(resp, f)


def download_audio(video_id, platform='youtube', media_url=None):
    from . import platforms as _pf
    wd = work_dir(video_id)
    if shutil.disk_usage(wd).free < MIN_FREE_BYTES:
        return DownloadResult(False, error_kind='resource', reason='disk_low')
    target = wd / (video_id + '.m4a')
    if target.exists() and target.stat().st_size > 0:
        return DownloadResult(True, path=target)
    if platform == 'podcast':
        if not media_url:
            return DownloadResult(False, error_kind='data', reason='no_media_url')
        try:
            _http_download(media_url, target)
        except Exception as e:
            return DownloadResult(False, error_kind='transient', reason=str(e)[:200])
        if not target.exists() or target.stat().st_size == 0:
            return DownloadResult(False, error_kind='data', reason='empty_download')
        return DownloadResult(True, path=target)
    try:
        import yt_dlp
    except ImportError:
        return DownloadResult(False, error_kind='resource', reason='yt_dlp_not_installed')
    fmt = '140/bestaudio[ext=m4a]/bestaudio' if platform == 'youtube' else 'bestaudio/best'
    opts = {'quiet': True, 'no_warnings': True, 'format': fmt,
            'outtmpl': str(wd / (video_id + '.%(ext)s'))}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([_pf.watch_url(platform, video_id)])
    except yt_dlp.utils.DownloadError as e:
        return DownloadResult(False, error_kind=_classify_error(str(e)), reason=str(e)[:200])
    candidates = sorted(wd.glob(video_id + '.m4a')) or sorted(wd.glob(video_id + '.*'))
    audio = next((p for p in candidates if p.suffix != '.part'), None)
    if audio is None or audio.stat().st_size == 0:
        return DownloadResult(False, error_kind='data', reason='empty_download')
    return DownloadResult(True, path=audio)
