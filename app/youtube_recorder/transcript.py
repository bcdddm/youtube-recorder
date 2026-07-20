"""Canonical timestamped transcript + validator (v0.2 §2.4, P1 spike findings).

Real defects this must catch (observed 2026-07-19 on nnze4i2Mt6o):
- trailing Whisper hallucination ("Продолжение следует...")
- final timestamp beyond video duration (1105.7s > 1078s)
Plus: repeated-loop segments, empty/garbage SRT, non-monotonic times.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
DURATION_TOLERANCE_MS = 15_000
MIN_COVERAGE = 0.40          # speech share below this → suspicious
MAX_REPEAT_RUN = 4           # >4 identical consecutive texts → loop hallucination

KNOWN_HALLUCINATIONS = [
    "продолжение следует", "subtitles by", "amara.org", "字幕by",
    "thank you for watching", "thanks for watching", "ご視聴ありがとうございました",
    "다음 영상에서 만나요",
]

_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_SRT_BLOCK = re.compile(
    r"(?:^|\n)\s*\d*\s*\n?(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.S)


class TranscriptInvalid(ValueError):
    pass


@dataclass
class Segment:
    segment_id: str
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Canonical:
    video_id: str
    language: str
    duration_ms: int
    source: str
    segments: list[Segment]
    warnings: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    def coverage(self) -> float:
        if not self.duration_ms:
            return 1.0
        return sum(s.end_ms - s.start_ms for s in self.segments) / self.duration_ms

    def to_json(self) -> str:
        return json.dumps({
            "schema_version": SCHEMA_VERSION,
            "video_id": self.video_id,
            "language": self.language,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "coverage": round(self.coverage(), 4),
            "warnings": self.warnings,
            "segments": [s.__dict__ for s in self.segments],
        }, ensure_ascii=False, indent=1)

    @staticmethod
    def from_json(text: str) -> "Canonical":
        d = json.loads(text)
        return Canonical(
            video_id=d["video_id"], language=d["language"],
            duration_ms=d["duration_ms"], source=d["source"],
            segments=[Segment(**s) for s in d["segments"]],
            warnings=d.get("warnings", []),
        )


def _ms(ts: str) -> int:
    m = _TS.fullmatch(ts.replace(".", ","))
    if not m:
        raise TranscriptInvalid(f"bad timestamp {ts!r}")
    h, mi, s, ms = map(int, m.groups())
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


def parse_subtitle_file(path: Path) -> list[tuple[int, int, str]]:
    """Parse SRT or VTT into raw (start_ms, end_ms, text) tuples."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("WEBVTT"):
        text = re.sub(r"^WEBVTT.*?\n\n", "", text, flags=re.S)  # strip header
    out = []
    for m in _SRT_BLOCK.finditer(text):
        body = re.sub(r"<[^>]+>", "", m.group(3)).strip()  # strip vtt tags
        body = re.sub(r"\s*\n\s*", " ", body)
        if body:
            out.append((_ms(m.group(1)), _ms(m.group(2)), body))
    return out


def _dominant_script(segments: list[tuple[int, int, str]]) -> str:
    counts: dict[str, int] = {}
    for _, _, t in segments[: min(len(segments), 200)]:
        for ch in t[:80]:
            if ch.isalpha():
                try:
                    name = unicodedata.name(ch).split()[0]
                except ValueError:
                    continue
                counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else "UNKNOWN"


def _is_hallucination(text: str, dominant: str) -> bool:
    low = text.lower().strip()
    if any(k in low for k in KNOWN_HALLUCINATIONS):
        return True
    # short isolated tail in a different script than the rest of the video
    letters = [c for c in text if c.isalpha()]
    if letters and len(text) < 60:
        try:
            script = unicodedata.name(letters[0]).split()[0]
        except ValueError:
            return False
        if dominant != "UNKNOWN" and script not in (dominant,):
            return True
    return False


def canonicalize(video_id: str, sub_path: Path, *, duration_sec: int,
                 source: str, language: str = "auto") -> Canonical:
    raw = parse_subtitle_file(sub_path)
    if not raw:
        raise TranscriptInvalid("no segments parsed")
    duration_ms = duration_sec * 1000
    warnings: list[str] = []

    # sort + drop non-monotonic anomalies
    raw.sort(key=lambda x: x[0])

    # duration bound (spike defect #2)
    if duration_ms:
        kept = [s for s in raw if s[0] <= duration_ms + DURATION_TOLERANCE_MS]
        if len(kept) < len(raw):
            warnings.append(f"dropped_{len(raw)-len(kept)}_beyond_duration")
        raw = kept
        raw = [(a, min(b, duration_ms + DURATION_TOLERANCE_MS), t) for a, b, t in raw]

    # collapse loop hallucinations
    collapsed: list[tuple[int, int, str]] = []
    run = 0
    for seg in raw:
        if collapsed and seg[2] == collapsed[-1][2]:
            run += 1
            if run >= MAX_REPEAT_RUN:
                continue
        else:
            run = 0
        collapsed.append(seg)
    if len(collapsed) < len(raw):
        warnings.append(f"collapsed_{len(raw)-len(collapsed)}_repeats")

    # trailing hallucination trim (spike defect #1): inspect last 3 segments
    dominant = _dominant_script(collapsed)
    while collapsed and _is_hallucination(collapsed[-1][2], dominant):
        warnings.append(f"trimmed_tail_hallucination:{collapsed[-1][2][:40]}")
        collapsed.pop()
    if not collapsed:
        raise TranscriptInvalid("all segments removed by validation")

    # invalid ranges
    good = [(a, b, t) for a, b, t in collapsed if b > a]
    if len(good) < len(collapsed):
        warnings.append(f"dropped_{len(collapsed)-len(good)}_zero_length")

    segments = [Segment(f"s{i:04d}", a, b, t) for i, (a, b, t) in enumerate(good)]
    can = Canonical(video_id=video_id, language=language,
                    duration_ms=duration_ms, source=source,
                    segments=segments, warnings=warnings)
    cov = can.coverage()
    if duration_ms and cov < MIN_COVERAGE:
        raise TranscriptInvalid(f"coverage too low: {cov:.2f}")
    return can


def save_canonical(can: Canonical, dest_dir: Path) -> Path:
    dest = dest_dir / "transcript.canonical.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(can.to_json(), encoding="utf-8")
    tmp.replace(dest)
    return dest
