"""OpenAI 语音转译适配器（无 MacWhisper 模式 / 超时兜底）。

流程（v0.2 §3.3 实装）：
  1. 音频 > 上限 → ffmpeg 压缩为 16kHz 单声道 32kbps AAC（转录质量不受影响）
  2. 仍超限 → 按时长切段，相邻段之间保留 overlap（默认 15 秒）重叠
  3. 各段调 OpenAI API（verbose_json 拿分段时间码），时间码加上段偏移还原为全局
  4. 重叠区按时间码对齐合并：切点取重叠中点，前段负责切点之前、后段负责之后，
     跨切点的句子归前段（前段音频完整覆盖到重叠末尾），无需 LLM 拼接
  5. 输出标准 SRT 到 work 目录，交给下游 canonicalize 统一校验
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
DEFAULT_MAX_MB = 24  # OpenAI 上限 25MB，留余量


class OpenAIAudioError(RuntimeError):
    def __init__(self, msg: str, transient: bool = True):
        super().__init__(msg)
        self.transient = transient


# --- 纯函数：切段规划与合并（可测） ------------------------------------------

def plan_chunks(duration_sec: float, size_bytes: int, max_bytes: int,
                overlap_sec: float = 15.0) -> list[tuple[float, float]]:
    """返回 [(start_sec, dur_sec)]。单段放得下就返回一段。
    相邻段重叠 overlap_sec；段长按大小比例留 20% 余量。"""
    if size_bytes <= max_bytes:
        return [(0.0, duration_sec)]
    chunk_dur = max(60.0, duration_sec * (max_bytes / size_bytes) * 0.8)
    step = chunk_dur - overlap_sec
    if step <= 0:
        raise OpenAIAudioError("chunk duration <= overlap", transient=False)
    out = []
    start = 0.0
    while start < duration_sec:
        out.append((start, min(chunk_dur, duration_sec - start)))
        if start + chunk_dur >= duration_sec:
            break
        start += step
    return out


def merge_chunk_segments(per_chunk: list[tuple[float, list[tuple[float, float, str]]]],
                         overlap_sec: float = 15.0) -> list[tuple[float, float, str]]:
    """per_chunk: [(chunk_start_sec, [(seg_start,seg_end,text) 相对段内]), ...]
    时间码全局化后按切点（重叠中点）去重合并。"""
    merged: list[tuple[float, float, str]] = []
    for i, (cstart, segs) in enumerate(per_chunk):
        cut = cstart + overlap_sec / 2 if i > 0 else float("-inf")
        for s, e, t in segs:
            gs, ge = s + cstart, e + cstart
            if gs >= cut:
                merged.append((gs, ge, t.strip()))
    merged.sort(key=lambda x: x[0])
    return [m for m in merged if m[2]]


def to_srt(segments: list[tuple[float, float, str]]) -> str:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        return (f"{ms//3600000:02d}:{ms%3600000//60000:02d}:"
                f"{ms%60000//1000:02d},{ms%1000:03d}")
    blocks = []
    for i, (s, e, t) in enumerate(segments, 1):
        blocks.append(f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n")
    return "\n".join(blocks)


# --- ffmpeg 步骤 ---------------------------------------------------------------

def _compress(src: Path, dest: Path) -> Path:
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-b:a", "32k", "-c:a", "aac", str(dest)],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not dest.exists():
        raise OpenAIAudioError(f"ffmpeg compress failed: {r.stderr[:150]}",
                               transient=False)
    return dest


def _cut(src: Path, start: float, dur: float, dest: Path) -> Path:
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.2f}",
         "-t", f"{dur:.2f}", "-i", str(src), "-c", "copy", str(dest)],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dest.exists():
        raise OpenAIAudioError(f"ffmpeg cut failed: {r.stderr[:150]}",
                               transient=False)
    return dest


# --- 主入口 --------------------------------------------------------------------

def transcribe(cfg, con, video_id: str, audio_path: Path,
               duration_sec: float, work: Path) -> Path:
    """转译 audio_path，输出 transcript.original.srt 到 work，返回路径。"""
    from .creds import get_key
    _kp = cfg.get("transcription.audio_key", "openai") or "openai"
    key = get_key(_kp)
    if not key:
        raise OpenAIAudioError(f"no {_kp} key (Keychain: ytrec-{_kp})",
                               transient=False)
    try:
        from openai import OpenAI
    except ImportError as e:
        raise OpenAIAudioError(f"openai import failed: {e}", transient=False)
    _base = cfg.get("transcription.audio_base_url", "") or ""
    client = OpenAI(api_key=key, base_url=_base) if _base else OpenAI(api_key=key)
    model = cfg.get("transcription.api_model", "whisper-1")
    overlap = float(cfg.get("transcription.chunk_overlap_sec", 15))
    max_bytes = int(cfg.get("transcription.max_upload_mb", DEFAULT_MAX_MB)) * 1024 * 1024

    src = audio_path
    if src.stat().st_size > max_bytes:
        src = _compress(audio_path, work / "audio.compressed.m4a")

    chunks = plan_chunks(duration_sec, src.stat().st_size, max_bytes, overlap)
    per_chunk = []
    for i, (start, dur) in enumerate(chunks):
        part = src if len(chunks) == 1 else _cut(
            src, start, dur, work / f"audio.part{i:02d}.m4a")
        try:
            with open(part, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=model, file=f, response_format="verbose_json")
        except Exception as e:  # SDK 异常谱系宽，统一按可重试处理
            status = getattr(e, "status_code", None)
            raise OpenAIAudioError(
                f"openai transcribe http {status or e}",
                transient=status in (None, 429, 500, 502, 503))
        segs = [(float(s.start), float(s.end), s.text)
                for s in (resp.segments or [])]
        if not segs and getattr(resp, "text", ""):
            segs = [(0.0, dur, resp.text)]
        per_chunk.append((start, segs))

    merged = merge_chunk_segments(per_chunk, overlap)
    if not merged:
        raise OpenAIAudioError("empty transcription", transient=False)

    dest = work / "transcript.original.srt"
    tmp = dest.with_suffix(".srt.tmp")
    tmp.write_text(to_srt(merged), encoding="utf-8")
    tmp.replace(dest)

    if con is not None:
        from .db import now
        minutes = duration_sec / 60
        con.execute(
            "INSERT INTO costs(video_id,provider,model,units,unit_type,"
            "estimated_cost_usd,at) VALUES(?,?,?,?,?,?,?)",
            (video_id, "openai", model, round(minutes, 2), "audio_minutes",
             round(minutes * 0.006, 4), now()))
        con.commit()
    # 清理中间文件
    for p in work.glob("audio.part*.m4a"):
        p.unlink(missing_ok=True)
    (work / "audio.compressed.m4a").unlink(missing_ok=True)
    return dest
