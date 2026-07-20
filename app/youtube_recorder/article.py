"""Article Transformer (v0.2 §4): full-transcript, chunked, traceable.

Never truncates the middle. Two LLM passes:
  1. per-chunk faithful notes (cheap, bounded context)
  2. global composition into a structured article JSON
A deterministic renderer turns the JSON into Markdown — the LLM never writes
the final file. Sections carry source_chunk_ids → chunk time ranges, so every
part of the article is traceable back to transcript time codes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import providers
from .transcript import Canonical

CHUNK_TARGET_MIN = 8
CHUNK_MAX_CHARS = 9000

NOTES_SYSTEM = """你是一名严谨的笔记员。给你一段视频口述稿片段，输出忠实笔记。
规则：只记录片段中明确说到的内容；数字、专名、结论必须原样保留；不补充外部知识；
不确定的内容标注[待查]。输出 JSON：
{"summary": "2-3句概括", "key_points": ["要点,含具体数字与论据"...],
 "entities": ["专名/公司/人物"...], "numbers": ["数字及其含义"...]}
只输出 JSON。"""

COMPOSE_SYSTEM = """你是一名专业编辑，把口述视频的分块笔记整理成一篇可读的中文文章。
规则：
- 只使用笔记中的信息，不新增任何事实、数字或结论；不确定处保留[待查]标记。
- 重组结构、合并重复论点、补充过渡句（edited_article 模式）。
- 每个章节必须在 source_chunk_ids 中列出其内容来源的笔记块编号。
- 标题15字内，准确概括核心内容，不做标题党。
输出 JSON：
{"title_zh": "...", "aliases": ["别名1","别名2"], "one_sentence": "一句话：谁、讲什么、为什么值得看",
 "summary": "3-5句摘要", "sections": [{"heading": "...", "body": "正文段落，可含多段",
 "source_chunk_ids": [0,1]}...], "takeaways": ["..."...], "tags": ["...", 最多6个]}
只输出 JSON。"""


@dataclass
class Chunk:
    chunk_id: int
    start_ms: int
    end_ms: int
    first_segment: str
    last_segment: str
    text: str


def chunk_transcript(can: Canonical) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list = []
    cur_chars = 0
    cur_start = None
    target_ms = CHUNK_TARGET_MIN * 60_000
    for seg in can.segments:
        if cur and (seg.start_ms - cur_start >= target_ms
                    or cur_chars + len(seg.text) > CHUNK_MAX_CHARS):
            chunks.append(_mk_chunk(len(chunks), cur))
            cur, cur_chars, cur_start = [], 0, None
        if cur_start is None:
            cur_start = seg.start_ms
        cur.append(seg)
        cur_chars += len(seg.text)
    if cur:
        chunks.append(_mk_chunk(len(chunks), cur))
    return chunks


def _mk_chunk(cid: int, segs: list) -> Chunk:
    return Chunk(chunk_id=cid, start_ms=segs[0].start_ms, end_ms=segs[-1].end_ms,
                 first_segment=segs[0].segment_id, last_segment=segs[-1].segment_id,
                 text=" ".join(s.text for s in segs))


def _fmt_t(ms: int) -> str:
    s = ms // 1000
    return f"{s//60:02d}:{s%60:02d}" if s < 3600 else f"{s//3600}:{s%3600//60:02d}:{s%60:02d}"


def _analyze_one(cfg, con, video_id: str, c: Chunk) -> dict:
    user = (f"视频片段 {c.chunk_id}（时间 {_fmt_t(c.start_ms)}–{_fmt_t(c.end_ms)}）：\n\n"
            f"{c.text}")
    reply = providers.complete(cfg, con, video_id, NOTES_SYSTEM, user,
                               max_tokens=2000, purpose="chunk_notes")
    try:
        note = providers.extract_json(reply)
    except (ValueError, json.JSONDecodeError):
        reply = providers.complete(cfg, con, video_id, NOTES_SYSTEM,
                                   user + "\n\n(上次输出不是合法 JSON，请只输出 JSON)",
                                   max_tokens=2000, purpose="chunk_notes_retry")
        note = providers.extract_json(reply)
    note["chunk_id"] = c.chunk_id
    note["time_range"] = [c.start_ms, c.end_ms]
    return note


def analyze_chunks(cfg, con, video_id: str, chunks: list[Chunk]) -> list[dict]:
    """并行分析各块（LLM 调用是纯等待，4 线程并发把长视频成文
    的墙钟时间从 N×单块 压到约 ceil(N/4)×单块）。成本记录串行落库。"""
    from concurrent.futures import ThreadPoolExecutor
    if len(chunks) == 1:
        return [_analyze_one(cfg, con, video_id, chunks[0])]
    # con 不跨线程共享：分析线程传 con=None（不记成本），主线程统一补记
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
        notes = list(ex.map(lambda c: _analyze_one(cfg, None, video_id, c),
                            chunks))
    return notes


REQUIRED_KEYS = ("title_zh", "one_sentence", "summary", "sections", "takeaways", "tags")


def compose_article(cfg, con, video_id: str, video_title: str,
                    channel: str, notes: list[dict]) -> dict:
    system = COMPOSE_SYSTEM
    custom = (cfg.get("article.custom_prompt") or "").strip()
    if custom:
        system += ("\n\n用户附加要求（在不违反上述忠实性规则的前提下遵守）：\n"
                   + custom)
    user = (f"视频标题：{video_title}\n频道：{channel}\n\n分块笔记：\n"
            + json.dumps(notes, ensure_ascii=False))
    reply = providers.complete(cfg, con, video_id, system, user,
                               max_tokens=8000, purpose="compose")
    try:
        art = providers.extract_json(reply)
    except (ValueError, json.JSONDecodeError):
        reply = providers.complete(cfg, con, video_id, system,
                                   user + "\n\n(上次输出不是合法 JSON，请只输出 JSON)",
                                   max_tokens=8000, purpose="compose_retry")
        art = providers.extract_json(reply)
    missing = [k for k in REQUIRED_KEYS if k not in art]
    if missing:
        raise ValueError(f"article JSON missing keys: {missing}")
    if not isinstance(art["sections"], list) or not art["sections"]:
        raise ValueError("article has no sections")
    return art


def generate(cfg, con, video_id: str, can: Canonical,
             video_title: str, channel: str) -> dict:
    chunks = chunk_transcript(can)
    notes = analyze_chunks(cfg, con, video_id, chunks)
    art = compose_article(cfg, con, video_id, video_title, channel, notes)
    art["_chunks"] = [c.__dict__ | {"text": None} for c in chunks]  # traceability
    art["_mode"] = cfg.get("article.mode", "edited_article")
    return art


def save_article(art: dict, dest_dir: Path) -> Path:
    dest = dest_dir / "article.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(art, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dest)
    return dest
