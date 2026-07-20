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


SELECT_SYSTEM = """你是选句编辑。给你一段视频口述稿的带编号句子。
任务：挑选要"逐字保留"的句子（目标约占本段字符的 {pct}%），优先保留：
含具体数字/点位/结论的句子、论证链完整的连续句。选中的句子会被程序原样拷贝，
你不改写它们。另外给本段起一个小标题，并写一句不超过 30 字的过渡（不复述内容）。
输出 JSON：{{"heading": "小标题", "bridge": "过渡一句", "keep": ["s0001","s0002",...]}}
只输出 JSON。"""

META_SYSTEM = """根据文章的章节标题与摘句样本，生成元信息。不得编造材料外的事实。
输出 JSON：{"title_zh":"15字内标题","aliases":["别名"],"one_sentence":"一句话",
"summary":"3-5句摘要","takeaways":["要点"...],"tags":["标签",最多6个]}
只输出 JSON。"""


def _verbatim_sections(cfg, con, video_id, can, chunks, pct):
    """选句式生成：AI 只挑句+写过渡，原句程序拷贝 → 保留率硬保证。"""
    seg_by_id = {s.segment_id: s for s in can.segments}
    sections = []
    for c in chunks:
        seg_ids = []
        cur = False
        for seg in can.segments:
            if seg.segment_id == c.first_segment:
                cur = True
            if cur:
                seg_ids.append(seg.segment_id)
            if seg.segment_id == c.last_segment:
                break
        lines = "\n".join(f"{sid}: {seg_by_id[sid].text}" for sid in seg_ids)
        try:
            reply = providers.complete(
                cfg, con, video_id, SELECT_SYSTEM.format(pct=pct),
                lines[:14000], max_tokens=1500, purpose="chunk_notes")
            sel = providers.extract_json(reply)
        except Exception:
            sel = {"heading": "", "bridge": "", "keep": seg_ids}
        keep = [k for k in sel.get("keep", []) if k in seg_by_id and k in seg_ids]
        if not keep:
            keep = seg_ids[: max(1, len(seg_ids) // 2)]
        sections.append({
            "heading": (sel.get("heading") or f"片段 {c.chunk_id + 1}")[:30],
            "bridge": ("" if pct >= 100 else (sel.get("bridge") or "")[:40]),
            "keep": keep, "all_ids": seg_ids,
            "source_chunk_ids": [c.chunk_id],
        })

    def measure():
        q = sum(len(seg_by_id[k].text) for sec in sections for k in sec["keep"])
        b = sum(len(sec["bridge"]) for sec in sections)
        total = q + b
        return (q / total if total else 1.0), q, b

    # 硬约束强制：不足则按顺序补选未保留的原句，直到达标
    ratio, _, _ = measure()
    target = pct / 100.0
    guard = 0
    while ratio < target and guard < 10000:
        added = False
        for sec in sections:
            extra = [i for i in sec["all_ids"] if i not in sec["keep"]]
            if extra:
                nxt = extra[0]
                pos = sec["all_ids"].index(nxt)
                sec["keep"] = sorted(set(sec["keep"]) | {nxt},
                                     key=sec["all_ids"].index)
                added = True
                ratio, _, _ = measure()
                if ratio >= target:
                    break
        if not added:  # 已全部保留仍不达标 → 去掉过渡句
            for sec in sections:
                sec["bridge"] = ""
            ratio, _, _ = measure()
            break
        guard += 1

    out = []
    for sec in sections:
        quote = "".join(seg_by_id[k].text for k in sec["keep"])
        body = (sec["bridge"] + "\n\n" + quote) if sec["bridge"] else quote
        out.append({"heading": sec["heading"], "body": body,
                    "source_chunk_ids": sec["source_chunk_ids"]})
    ratio, q, b = measure()
    return out, round(ratio, 3)


def generate(cfg, con, video_id: str, can: Canonical,
             video_title: str, channel: str) -> dict:
    chunks = chunk_transcript(can)
    pct = int(cfg.get("article.verbatim_pct", 70) or 0)
    if pct >= 50:
        sections, ratio = _verbatim_sections(cfg, con, video_id, can, chunks, pct)
        sample = "\n".join(
            f"## {s['heading']}\n{s['body'][:200]}" for s in sections)[:8000]
        try:
            reply = providers.complete(cfg, con, video_id, META_SYSTEM,
                                       f"视频标题：{video_title}\n频道：{channel}\n\n"
                                       + sample, max_tokens=1500, purpose="compose")
            meta = providers.extract_json(reply)
        except Exception:
            meta = {}
        art = {
            "title_zh": (meta.get("title_zh") or video_title)[:40],
            "aliases": meta.get("aliases", []),
            "one_sentence": meta.get("one_sentence", ""),
            "summary": meta.get("summary", ""),
            "sections": sections,
            "takeaways": meta.get("takeaways", []),
            "tags": meta.get("tags", []),
            "verbatim_pct": pct,
            "verbatim_ratio": ratio,
        }
    else:
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
