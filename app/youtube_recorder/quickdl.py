"""独立的"粘贴链接直接下载"功能——与转录/整理管线完全分开。

用户在 GUI 里粘贴任意 yt-dlp 支持的视频链接，选清晰度，后台线程下载到
downloads.dest_dir。用内存字典跟踪进度（GUI 页面轮询 JSON），不落数据库、
不进 videos 状态机。
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
MAX_JOBS = 30  # 内存中最多保留的历史任务数（含进行中）

_URL_RE = re.compile(r"^https?://", re.I)

QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "2160p": "bv*[height<=2160]+ba/b[height<=2160]",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    "audio": "bestaudio/best",
}


def valid_url(url: str) -> bool:
    return bool(url and _URL_RE.match(url.strip()) and len(url) < 2000)


def _new_job() -> str:
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(_JOBS) >= MAX_JOBS:
            # 清掉最早的已结束任务腾位置
            done = sorted((j for j in _JOBS.values() if j["status"] in ("done", "error")),
                         key=lambda j: j["started"])
            for j in done[:max(1, len(_JOBS) - MAX_JOBS + 1)]:
                _JOBS.pop(j["id"], None)
        _JOBS[jid] = {"id": jid, "status": "queued", "pct": 0.0, "speed": "",
                     "eta": "", "title": "", "error": "", "path": "",
                     "started": time.time(), "url": ""}
    return jid


def get_job(jid: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(jid)
        return dict(j) if j else None


def list_jobs() -> list[dict]:
    with _LOCK:
        return sorted((dict(j) for j in _JOBS.values()),
                     key=lambda j: -j["started"])


def start_download(url: str, quality: str, dest_dir: Path) -> str:
    jid = _new_job()
    with _LOCK:
        _JOBS[jid]["url"] = url
        _JOBS[jid]["status"] = "downloading"
    t = threading.Thread(target=_run, args=(jid, url, quality, dest_dir), daemon=True)
    t.start()
    return jid


_ERROR_PATTERNS = [
    # (匹配子串, 用户可读原因)
    ("Unsupported URL", "无法识别这个链接（不是可下载的视频页面，或该网站暂不支持）"),
    ("is not a valid URL", "链接格式不正确"),
    ("Unable to extract", "无法解析该页面的视频信息（页面结构可能已变化或非视频页）"),
    ("Video unavailable", "视频不可用（可能已被删除、下架或地区限制）"),
    ("Private video", "这是私享视频，无法下载"),
    ("This video is private", "这是私享视频，无法下载"),
    ("Sign in to confirm", "该视频需要登录验证（年龄限制等），暂不支持"),
    ("age", "该视频有年龄限制，暂不支持"),
    ("members-only", "这是会员专属内容，无法下载"),
    ("copyright", "该内容因版权原因不可下载"),
    ("blocked it in your country", "该视频在你所在地区不可用"),
    ("live event will begin", "这是尚未开始的直播预告，暂无法下载"),
    ("HTTP Error 404", "找不到该视频（链接可能已失效）"),
    ("HTTP Error 403", "被目标网站拒绝访问（403）"),
    ("Requested format is not available", "找不到符合所选清晰度的视频流，请换一档清晰度重试"),
    ("ffmpeg not found", "缺少 ffmpeg，无法合并音视频"),
    ("No space left", "磁盘空间不足"),
    ("磁盘空间不足", "磁盘空间不足"),
    ("Name or service not known", "网络连接失败（DNS 解析失败），请检查网络"),
    ("Temporary failure in name resolution", "网络连接失败，请检查网络"),
    ("timed out", "网络连接超时，请检查网络后重试"),
    ("Connection refused", "网络连接被拒绝，请稍后重试"),
]


def _friendly_error(raw: str, parsed_ok: bool) -> str:
    """把 yt-dlp/系统异常翻成用户能看懂的中文原因；parsed_ok=False 表示
    连视频信息都没解析出来（用于区分"解析失败"还是"下载失败"）。"""
    for pat, human in _ERROR_PATTERNS:
        if pat.lower() in (raw or "").lower():
            return human
    prefix = "解析失败" if not parsed_ok else "下载失败"
    detail = (raw or "未知错误").strip().splitlines()[0][:180]
    return f"{prefix}：{detail}"


def _run(jid: str, url: str, quality: str, dest_dir: Path) -> None:
    parsed_ok = {"v": False}  # extract_info 是否成功拿到过视频信息

    def hook(d: dict) -> None:
        with _LOCK:
            j = _JOBS.get(jid)
            if not j:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                j["pct"] = round(done / total * 100, 1) if total else j["pct"]
                spd = d.get("speed")
                j["speed"] = f"{spd/1024/1024:.1f} MB/s" if spd else ""
                eta = d.get("eta")
                j["eta"] = f"{eta}s" if eta else ""
                info = d.get("info_dict") or {}
                if info.get("title"):
                    j["title"] = info["title"][:120]
            elif d.get("status") == "finished":
                j["pct"] = 100.0
                j["status"] = "merging"

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(dest_dir).free < 500 * 1024 * 1024:
            raise RuntimeError("磁盘空间不足（剩余 < 500MB）")
        import yt_dlp
        fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["1080p"])
        opts = {
            "quiet": True, "no_warnings": True, "format": fmt,
            "outtmpl": str(dest_dir / "%(title).150B [%(id)s].%(ext)s"),
            "ffmpeg_location": str(Path(FFMPEG).parent),
            "progress_hooks": [hook],
            "merge_output_format": "mp4" if quality != "audio" else None,
            "noplaylist": True,
            "restrictfilenames": False,
        }
        with yt_dlp.YoutubeDL({k: v for k, v in opts.items() if v is not None}) as ydl:
            info = ydl.extract_info(url, download=True)
            parsed_ok["v"] = True
            fname = ydl.prepare_filename(info)
            # merge_output_format 可能改变最终扩展名
            p = Path(fname)
            if quality != "audio" and p.suffix != ".mp4":
                cand = p.with_suffix(".mp4")
                if cand.exists():
                    p = cand
        with _LOCK:
            j = _JOBS.get(jid)
            if j:
                j["status"] = "done"
                j["pct"] = 100.0
                j["path"] = str(p)
                if not j["title"]:
                    j["title"] = (info.get("title") or "")[:120]
    except Exception as e:
        with _LOCK:
            j = _JOBS.get(jid)
            if j:
                j["status"] = "error"
                j["error"] = _friendly_error(str(e), parsed_ok["v"])
