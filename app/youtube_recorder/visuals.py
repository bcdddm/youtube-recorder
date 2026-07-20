"""Visual Evidence Pipeline (v0.2 §5, MVP).

recall(宁多) → density gating → clip download (yt-dlp --download-sections)
→ multi-frame extraction (t-2 / t / t+2, ffmpeg) → sharpest-frame pick
→ perceptual-hash dedup → visual-plan.json.

MVP notes (v0.3 backlog): vision-model relevance QA is stubbed behind
`require_vision_confirmation` (default off); sharpness = Laplacian-variance
if Pillow is present, else file-size proxy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# --- recall rules -------------------------------------------------------------

EXPLICIT_CUES = [
    r"看(?:这张|这个|一下|下面的?|上面的?)?(?:图|表|表格|图表|数据|画面|屏幕)",
    r"(?:这张|这个|如)(?:图|表|表格|图表)", r"幻灯片", r"给大家看", r"我们来看",
    r"as you can see", r"on (?:the )?screen", r"this (?:chart|table|graph|slide)",
    r"look at (?:this|the)", r"shown here",
]
IMPLICIT_CUES = [
    r"第[一二三四五六七八九十\d]+[列行栏]", r"[红蓝绿黄]色?的?(?:线|柱|区域|框)",
    r"K线", r"均线", r"走势图", r"曲线", r"这个按钮", r"这段代码", r"左边|右边的?(?:图|栏|列)",
    r"箭头", r"高亮", r"圈出来",
]
_EXPLICIT = re.compile("|".join(EXPLICIT_CUES), re.I)
_IMPLICIT = re.compile("|".join(IMPLICIT_CUES), re.I)

# density 1..5 → (candidate_threshold, min_spacing_sec, soft_max) — v0.2 §9.5
DENSITY = {
    1: (0.90, 90, 3),
    2: (0.82, 60, 5),
    3: (0.72, 35, 8),
    4: (0.62, 20, 15),
    5: (0.50, 0, 999),
}


@dataclass
class Candidate:
    candidate_id: str
    segment_id: str
    chunk_id: int | None
    target_ms: int
    window_ms: tuple[int, int]
    cue: str
    confidence: float
    selected_frame: str | None = None
    frame_time_ms: int | None = None
    status: str = "candidate"   # candidate|selected|rejected|no_usable_frame
    reason: str = ""


LLM_RECALL_SYSTEM = """你是视频画面分析助手。给你一个视频的口述文稿（带时间点），
判断讲者在哪些时刻最可能正在屏幕上展示图表、表格、财报数据、幻灯片或代码等视觉内容。
线索：讲者密集报出具体数字/百分比/金额时，屏幕通常在展示对应表格；提到"往下看/这里/对比"
等指代时通常有画面。输出 JSON 数组（按把握排序，最多 {max_n} 个）：
[{{"time_ms": 毫秒时间点, "reason": "为什么此刻可能有画面", "confidence": 0到1}}]
只输出 JSON 数组。没有合适时刻就输出 []。"""


def llm_recall(cfg, con, video_id: str, can, chunks, density: int) -> list[Candidate]:
    """规则召回为空时的兜底：LLM 根据内容推断展示画面的时刻。"""
    from . import providers
    thr, spacing, soft_max = DENSITY.get(density, DENSITY[3])
    max_n = min(soft_max, 10)
    lines = []
    for s in can.segments[::2]:  # 隔行采样控制 token
        lines.append(f"[{s.start_ms}] {s.text}")
    user = "\n".join(lines)[:16000]
    try:
        reply = providers.complete(cfg, con, video_id,
                                   LLM_RECALL_SYSTEM.format(max_n=max_n),
                                   user, max_tokens=1500, purpose="visual_recall")
        import json as _json
        import re as _re
        m = _re.search(r"\[.*\]", reply, _re.S)
        items = _json.loads(m.group(0)) if m else []
    except Exception:
        return []
    ranges = [(c.chunk_id, c.start_ms, c.end_ms) for c in chunks]

    def chunk_of(ms):
        for cid, a, b in ranges:
            if a <= ms <= b:
                return cid
        return chunks[-1].chunk_id if chunks else None

    out: list[Candidate] = []
    last_ms = -10**9
    for it in sorted(items, key=lambda x: x.get("time_ms", 0)):
        try:
            t = int(it["time_ms"])
            conf = float(it.get("confidence", 0.5))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= t <= can.duration_ms) or conf < 0.4:
            continue
        if t - last_ms < spacing * 1000 or len(out) >= max_n:
            continue
        out.append(Candidate(
            candidate_id=f"L{len(out):03d}", segment_id="llm",
            chunk_id=chunk_of(t), target_ms=t,
            window_ms=(max(0, t - 4000), t + 6000),
            cue=str(it.get("reason", "AI 推断画面"))[:40], confidence=conf))
        last_ms = t
    return out


def recall(can, chunks, density: int) -> list[Candidate]:
    """Rule-based recall over canonical segments; assigns chunk ids for
    article-section mapping; applies density threshold/spacing/soft_max."""
    thr, spacing, soft_max = DENSITY.get(density, DENSITY[3])
    ranges = [(c.chunk_id, c.start_ms, c.end_ms) for c in chunks]

    def chunk_of(ms: int):
        for cid, a, b in ranges:
            if a <= ms <= b:
                return cid
        return None

    out: list[Candidate] = []
    last_ms = -10**9
    for seg in can.segments:
        m = _EXPLICIT.search(seg.text)
        conf, cue = (0.90, m.group(0)) if m else (0.0, "")
        if not m:
            m2 = _IMPLICIT.search(seg.text)
            if m2:
                conf, cue = 0.62, m2.group(0)
        if conf < thr:
            continue
        if seg.start_ms - last_ms < spacing * 1000:
            continue
        if len(out) >= soft_max:
            break
        out.append(Candidate(
            candidate_id=f"c{len(out):03d}", segment_id=seg.segment_id,
            chunk_id=chunk_of(seg.start_ms), target_ms=seg.start_ms,
            window_ms=(max(0, seg.start_ms - 3000), seg.end_ms + 5000),
            cue=cue, confidence=conf))
        last_ms = seg.start_ms
    return out


# --- frame extraction ----------------------------------------------------------

def _sec(ms: int) -> str:
    return f"{ms/1000:.1f}"


def download_clip(video_id: str, window_ms: tuple[int, int], dest: Path) -> Path | None:
    """Low-res clip for one candidate window via yt-dlp --download-sections."""
    try:
        import yt_dlp
    except ImportError:
        return None
    a, b = window_ms
    out = dest / f"clip-{a}.mp4"
    if out.exists():
        return out
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        # format 18 = progressive 360p MP4 (single stream, ffmpeg-seekable);
        # DASH-only formats fail when ffmpeg cuts sections over HTTPS.
        "format": "18/worst[ext=mp4][vcodec!=none][acodec!=none]/worst",
        "download_ranges": yt_dlp.utils.download_range_func(
            None, [(a / 1000, b / 1000 + 1)]),
        "outtmpl": str(out).replace(".mp4", ".%(ext)s"),
        "force_keyframes_at_cuts": False,
        "ffmpeg_location": str(Path(FFMPEG).parent),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception:
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def extract_frames(clip: Path, rel_target_s: float, dest: Path,
                   offsets=(-2.0, 0.0, 2.0)) -> list[Path]:
    frames = []
    for i, off in enumerate(offsets):
        t = max(0.0, rel_target_s + off)
        f = dest / f"{clip.stem}-f{i}.jpg"
        r = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
             str(clip), "-frames:v", "1", "-q:v", "3", str(f)],
            capture_output=True, timeout=60)
        if r.returncode == 0 and f.exists() and f.stat().st_size > 3000:
            frames.append(f)
    return frames


def sharpness(path: Path) -> float:
    """Laplacian variance if Pillow available, else size proxy."""
    try:
        from PIL import Image, ImageFilter
        import statistics
        im = Image.open(path).convert("L").resize((320, 180))
        lap = im.filter(ImageFilter.FIND_EDGES)
        vals = list(lap.getdata())
        return statistics.pvariance(vals)
    except Exception:
        return float(path.stat().st_size)


def ahash(path: Path) -> int:
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((8, 8))
        px = list(im.getdata())
        avg = sum(px) / 64
        bits = 0
        for i, p in enumerate(px):
            if p > avg:
                bits |= 1 << i
        return bits
    except Exception:
        return path.stat().st_size  # weak fallback


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def pick_frames(video_id: str, candidates: list[Candidate], work: Path,
                strict_fill: bool = False) -> list[Candidate]:
    """Download windows, extract multi-frames, keep sharpest, dedupe by aHash."""
    clips_dir = work / "clips"
    frames_dir = work / "frames"
    clips_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    kept_hashes: list[int] = []
    for c in candidates:
        clip = download_clip(video_id, c.window_ms, clips_dir)
        if clip is None:
            c.status, c.reason = "no_usable_frame", "clip_download_failed"
            continue
        rel = (c.target_ms - c.window_ms[0]) / 1000
        frames = extract_frames(clip, rel, frames_dir)
        if not frames:
            c.status, c.reason = "no_usable_frame", "no_frames_extracted"
            continue
        best = max(frames, key=sharpness)
        h = ahash(best)
        if not strict_fill and any(hamming(h, k) <= 5 for k in kept_hashes):
            c.status, c.reason = "rejected", "duplicate_of_kept_frame"
            continue
        kept_hashes.append(h)
        final = frames_dir / f"{video_id}--{_hhmmss(c.target_ms)}.jpg"
        shutil.copyfile(best, final)
        c.selected_frame = str(final)
        c.frame_time_ms = c.target_ms
        c.status = "selected"
    # cleanup clips (retention: keep_video_segments_days handled by janitor later)
    return candidates


def _hhmmss(ms: int) -> str:
    s = ms // 1000
    return f"{s//3600:02d}{s%3600//60:02d}{s%60:02d}"


def fill_candidates(cands: list[Candidate], chunks,
                    sections_per_chunk: dict) -> list[Candidate]:
    """密度 5 保障：每个文章小节至少一个候选。某块候选数少于该块小节数时，
    在块时间范围内均匀补造候选（程序生成，无需提示词命中）。"""
    by_chunk: dict = {}
    for c in cands:
        by_chunk.setdefault(c.chunk_id, []).append(c)
    out = list(cands)
    n_extra = 0
    for ch in chunks:
        need = sections_per_chunk.get(ch.chunk_id, 0)
        have = len(by_chunk.get(ch.chunk_id, []))
        if have >= need or need == 0:
            continue
        span = max(1, ch.end_ms - ch.start_ms)
        for i in range(need - have):
            frac = (have + i + 1) / (need + 1)
            t = ch.start_ms + int(span * frac)
            out.append(Candidate(
                candidate_id=f"F{n_extra:03d}", segment_id="fill",
                chunk_id=ch.chunk_id, target_ms=t,
                window_ms=(max(0, t - 4000), t + 6000),
                cue="该段时段画面", confidence=0.5))
            n_extra += 1
    out.sort(key=lambda c: c.target_ms)
    return out


def save_plan(candidates: list[Candidate], work: Path) -> Path:
    dest = work / "visual-plan.json"
    dest.write_text(json.dumps([asdict(c) for c in candidates],
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return dest


def load_plan(work: Path) -> list[dict]:
    p = work / "visual-plan.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))
