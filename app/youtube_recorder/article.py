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
MAX_SECTION_CHARS = 600   # 单节正文上限（约 2 分钟内读完）

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
- 分段规则：每个独立事件/话题单独成一节，不要把多个事件混在一节里；
  每节正文不超过 600 字（保证 2 分钟内能读完一节）。
- 每个章节必须在 source_chunk_ids 中列出其内容来源的笔记块编号。
- 标题15字内，准确概括核心内容，不做标题党。
- 标签分两类，分别输出到 tags 和 companies 两个数组，不要混在一起：
  · tags：概念/主题标签（如"美联储""财报解读""半导体""技术分析"），
    若给了"已有标签库"，优先从中选择贴切的复用，不要为同一概念反复新造
    近义词；确实没有合适的现有标签才新造，且每篇文章最多新造 1 个新
    标签；最多 6 个。
  · companies：文章提到的具体公司/股票代码/具名人物/具名产品（如
    "英伟达""TSLA""鲍威尔""ChatGPT"），不算概念标签，不要塞进 tags；
    若给了"已有公司库"同样优先复用同一写法（如已有"英伟达"就不要再写
    "NVIDIA"）；最多 6 个，没有就输出空数组。
输出 JSON：
{"title_zh": "...", "aliases": ["别名1","别名2"], "one_sentence": "一句话：谁、讲什么、为什么值得看",
 "summary": "3-5句摘要", "sections": [{"heading": "...", "body": "正文段落，可含多段",
 "source_chunk_ids": [0,1]}...], "takeaways": ["..."...], "tags": ["...", 最多6个],
 "companies": ["...", 最多6个]}
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

MAX_TAG_VOCAB = 150  # 注入 prompt 的已有标签数上限，按使用频次取前 N 个，控制 token 开销


def _vocab_from_field(con, field: str, max_n: int) -> list[str]:
    """通用实现：汇总所有历史文章 article.json 里某个数组字段（tags 或
    companies）出现过的值，按使用频次降序取前 N 个。tags 字段额外经
    tags-merge.json 的归并映射合并同义词；companies 字段没有归并表，
    原样按去重后的写法计数（保证以后新文章优先复用同一写法）。"""
    if not con:
        return []
    try:
        from .paths import APP_SUPPORT, work_dir
        rows = con.execute(
            "SELECT DISTINCT video_id FROM writes WHERE note_kind='wiki'").fetchall()
        tmap = {}
        if field == "tags":
            try:
                data = json.loads((APP_SUPPORT / "tags-merge.json").read_text(encoding="utf-8"))
                tmap = data.get("map", {}) if isinstance(data, dict) else {}
            except Exception:
                pass
        counts: dict[str, int] = {}
        for r in rows:
            aj = work_dir(r["video_id"]) / "article.json"
            if not aj.exists():
                continue
            try:
                vs = json.loads(aj.read_text(encoding="utf-8")).get(field, [])[:6]
            except Exception:
                continue
            for v in vs:
                if isinstance(v, str) and v.strip():
                    canon = tmap.get(v, v)
                    counts[canon] = counts.get(canon, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [t for t, _ in ranked[:max_n]]
    except Exception:
        return []


def existing_tag_vocab(con) -> list[str]:
    """已有文章用过的概念标签词表（按使用频次降序），成文打标签时优先
    复用，避免同一概念反复造近义词。读取失败/无历史文章时返回空表，
    不影响正常生成（此时相当于没有约束，和原来行为一致）。"""
    return _vocab_from_field(con, "tags", MAX_TAG_VOCAB)


def existing_company_vocab(con) -> list[str]:
    """已有文章提到过的公司/股票代码/具名人物词表，成文时优先复用同一
    写法（如已有"英伟达"就不要再新造"NVIDIA"）。和概念标签分开维护，
    不占用 100 个概念标签的名额。"""
    return _vocab_from_field(con, "companies", MAX_TAG_VOCAB)


def compose_article(cfg, con, video_id: str, video_title: str,
                    channel: str, notes: list[dict],
                    group_prompt: str = "", existing_tags: list[str] | None = None,
                    existing_companies: list[str] | None = None) -> dict:
    system = COMPOSE_SYSTEM
    custom = (cfg.get("article.custom_prompt") or "").strip()
    if custom:
        system += ("\n\n用户附加要求（在不违反上述忠实性规则的前提下遵守）：\n"
                   + custom)
    if group_prompt:
        system += ("\n\n所属组的个性化要求（在不违反忠实性规则的前提下遵守）：\n"
                   + group_prompt)
    user = (f"视频标题：{video_title}\n频道：{channel}\n\n分块笔记：\n"
            + json.dumps(notes, ensure_ascii=False))
    if existing_tags:
        user += "\n\n已有标签库（打标签时优先复用，不要新造近义词）：\n" + "、".join(existing_tags)
    if existing_companies:
        user += "\n\n已有公司库（提到同一公司/实体时优先复用同一写法）：\n" + "、".join(existing_companies)
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
    if not isinstance(art.get("companies"), list):
        art["companies"] = []
    return art


REORDER_BELOW = 70  # 低于此档位允许 AI 重排保留句顺序

SELECT_SYSTEM = """你是选句编辑。给你一段视频口述稿的带编号句子。
{order_rule}
两个任务：
1. 按"事件/话题"把本段切成 1-4 个小节：每个独立事件单独一节，不混谈；
   每节保留句总字数控制在 600 字以内。
2. 每节挑选要"逐字保留"的句子（整体目标约占本段字符的 {pct}%），优先保留
   含具体数字/点位/结论的句子、论证链完整的连续句。选中句由程序原样拷贝。
   完全没有信息量的句子（寒暄、口播广告、与主题无关的闲聊、纯重复）不要选入 keep。
每节给小标题和一句 ≤30 字过渡（不复述内容）。
输出 JSON：{{"sections": [{{"heading": "小标题", "bridge": "过渡",
"keep": ["s0001","s0002",...]}}, ...]}}
只输出 JSON。"""

META_SYSTEM = """根据文章的章节标题与摘句样本，生成元信息。不得编造材料外的事实。
标签分两类，分别输出到 tags 和 companies 两个数组，不要混在一起：
· tags：概念/主题标签，若给了"已有标签库"，优先从中选择贴切的复用，不要
  为同一概念反复新造近义词；确实没有合适的现有标签才新造，且每篇文章
  最多新造 1 个新标签；最多 6 个。
· companies：文章提到的具体公司/股票代码/具名人物/具名产品，不算概念
  标签，不要塞进 tags；若给了"已有公司库"同样优先复用同一写法；最多 6
  个，没有就输出空数组。
输出 JSON：{"title_zh":"15字内标题","aliases":["别名"],"one_sentence":"一句话",
"summary":"3-5句摘要","takeaways":["要点"...],"tags":["标签",最多6个],
"companies":["...",最多6个]}
只输出 JSON。"""


import re as _re

# 保留句的确定性清洗：不经 AI，硬约束不破——
# 只删纯语气词、折叠重复叠词、补标点（相当于"最多一两个词的改动"）
_FILLER_RE = _re.compile(
    r"(?:^|(?<=[，。！？、；\s]))(?:呃+|嗯+|唉+|啊+|哎+|嘛|[eE]mm+)"
    r"(?=$|[，。！？、；\s])")
_DUP_RE = _re.compile(r"(就是|然后|那个|这个|所以|但是)\1+")
_ANY_PUNCT = "。！？，、；：…"


def _clean_quote(t: str) -> str:
    t = _re.sub(r"\s+", " ", (t or "").strip())
    t = _FILLER_RE.sub("", t)
    t = _DUP_RE.sub(r"\1", t)
    t = _re.sub(r"\s{2,}", " ", t)          # 删词后残留的双空格
    t = _re.sub(r"\s+([，。！？、；：])", r"\1", t)
    t = _re.sub(r"^[，、,;\s]+", "", t)
    t = _re.sub(r"[，、,\s]+$", "", t)
    return t


def _join_quotes(texts: list[str]) -> str:
    """相邻保留句拼接：句中缺标点补"，"，收尾补"。"。"""
    parts = [t for t in texts if t]
    out = []
    for i, t in enumerate(parts):
        if t[-1] not in _ANY_PUNCT:
            t += "。" if i == len(parts) - 1 else "，"
        out.append(t)
    return "".join(out)


PUNCT_SYSTEM = """你是标点编辑。为给定中文文本重新标点：句号、逗号、问号、
感叹号、顿号、冒号、引号均可自由使用与调整，长句可在语义边界断句。
铁律：不得增加、删除或改动任何非标点字符——一个字都不行。
只输出重新标点后的文本，不要任何解释。"""


def _strip_punct(t: str) -> str:
    return _re.sub(r"[\W_]+", "", t or "")


def _ai_punctuate(cfg, con, video_id: str, text: str) -> str:
    """AI 重标点 + 程序硬校验：剥掉标点后必须与原文逐字一致，
    否则整段拒收、回退机械标点结果。"""
    if not text.strip() or len(text) > 4000:
        return text
    try:
        out = providers.complete(cfg, con, video_id, PUNCT_SYSTEM,
                                 text, max_tokens=len(text) + 500,
                                 purpose="proofread").strip()
    except Exception:
        return text
    if out and _strip_punct(out) == _strip_punct(text):
        return out
    return text  # 校验不过 → 机械结果保底


def _punctuate_sections(cfg, con, video_id: str, sections: list) -> int:
    """并行为各节正文做 AI 标点（bridge 是 AI 文本本就有标点，一并处理无害）。"""
    from concurrent.futures import ThreadPoolExecutor
    bodies = [sec.get("body", "") for sec in sections]
    with ThreadPoolExecutor(max_workers=4) as ex:
        new = list(ex.map(lambda b: _ai_punctuate(cfg, None, video_id, b),
                          bodies))
    changed = 0
    for sec, nb in zip(sections, new):
        if nb != sec.get("body", ""):
            sec["body"] = nb
            changed += 1
    return changed


def _verbatim_sections(cfg, con, video_id, can, chunks, pct,
                       group_prompt: str = ""):
    """选句式生成：AI 只挑句+写过渡，原句程序拷贝（含确定性清洗）→ 保留率硬保证。"""
    _gp_suffix = (("\n\n所属组的个性化要求（选句与小节标题可参考，"
                   "但不得违反逐字保留与忠实性规则）：\n" + group_prompt)
                  if group_prompt else "")
    seg_by_id = {s.segment_id: s for s in can.segments}
    cleaned = {s.segment_id: _clean_quote(s.text) for s in can.segments}
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
        order_rule = ("句序规则：可以为叙事连贯对句子的前后顺序做重组——"
                      "keep 数组的排列顺序就是输出顺序。"
                      if pct < REORDER_BELOW else
                      "句序规则：保持句子在原文中的出现顺序，不重排。")
        try:
            reply = providers.complete(
                cfg, con, video_id,
                SELECT_SYSTEM.format(pct=pct, order_rule=order_rule) + _gp_suffix,
                lines[:14000], max_tokens=2000, purpose="chunk_notes")
            sel = providers.extract_json(reply)
        except Exception:
            sel = {}
        subs = sel.get("sections")
        if not isinstance(subs, list) or not subs:
            subs = [{"heading": sel.get("heading", ""),
                     "bridge": sel.get("bridge", ""),
                     "keep": sel.get("keep", seg_ids)}]
        claimed = set()
        for sub in subs[:4]:
            keep = [k for k in sub.get("keep", [])
                    if k in seg_by_id and k in seg_ids and k not in claimed]
            if not keep:
                continue
            claimed.update(keep)
            sections.append({
                "heading": (sub.get("heading") or f"片段 {c.chunk_id + 1}")[:30],
                "bridge": ("" if pct >= 100 else (sub.get("bridge") or "")[:40]),
                "keep": keep, "all_ids": seg_ids,
                "source_chunk_ids": [c.chunk_id],
            })
        if not any(sec["source_chunk_ids"] == [c.chunk_id] for sec in sections):
            sections.append({
                "heading": f"片段 {c.chunk_id + 1}",
                "bridge": "", "keep": seg_ids[: max(1, len(seg_ids) // 2)],
                "all_ids": seg_ids, "source_chunk_ids": [c.chunk_id],
            })

    def measure():
        q = sum(len(cleaned[k]) for sec in sections for k in sec["keep"])
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
                if pct < REORDER_BELOW:
                    sec["keep"] = list(sec["keep"]) + [nxt]  # 保持 AI 排序，补句附尾
                else:
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
        parts, cur, cur_len = [], [], 0
        for k in sec["keep"]:
            t = cleaned[k]
            if cur and cur_len + len(t) + 1 > MAX_SECTION_CHARS:
                parts.append(cur)
                cur, cur_len = [], 0
            cur.append(k)
            cur_len += len(t) + 1  # +1 计入拼接补的标点
        if cur:
            parts.append(cur)
        for i, part in enumerate(parts):
            quote = _join_quotes([cleaned[k] for k in part])
            bridge = sec["bridge"] if i == 0 else ""
            body = (bridge + "\n\n" + quote) if bridge else quote
            heading = sec["heading"] + ("" if i == 0 else f"（续{i}）")
            out.append({"heading": heading, "body": body,
                        "source_chunk_ids": sec["source_chunk_ids"]})
    ratio, q, b = measure()
    return out, round(ratio, 3)


PROOFREAD_SYSTEM = """你是校对员。检查给定正文中的错别字与语音转写造成的
同音字误写（如"在/再""做/作"、公司名/术语误拼）。只报你有把握的错误。
不改数字、不改风格、不做润色。输出 JSON 数组（最多 30 条）：
[{"find": "原文中的错误片段(≤8字)", "replace": "改正后"}]
没有错误就输出 []。只输出 JSON 数组。"""


def proofread_sections(cfg, con, video_id: str, sections: list) -> int:
    """成文后错别字校对：LLM 只报"查找→替换"对，程序做定点替换。
    安全阀：find≤8 字、长度差≤2、必须命中原文——防止借校对之名重写。"""
    body_all = "\n".join(sec.get("body", "") for sec in sections)[:30000]
    if not body_all.strip():
        return 0
    try:
        reply = providers.complete(cfg, con, video_id, PROOFREAD_SYSTEM,
                                   body_all, max_tokens=1500,
                                   purpose="proofread")
        import re as _re2
        m = _re2.search(r"\[.*\]", reply, _re2.S)
        fixes = json.loads(m.group(0)) if m else []
    except Exception:
        return 0
    applied = 0
    for fx in fixes[:30]:
        find = str(fx.get("find", ""))
        rep = str(fx.get("replace", ""))
        if (not find or find == rep or len(find) > 8
                or abs(len(find) - len(rep)) > 2):
            continue
        hit = False
        for sec in sections:
            if find in sec.get("body", ""):
                sec["body"] = sec["body"].replace(find, rep)
                hit = True
        if hit:
            applied += 1
    return applied


def group_prompt_for(cfg, con, channel_id: str | None) -> str:
    """按视频所属频道的组，拼出带组名标注的个性化 prompt（可属多组）。"""
    if not con or not channel_id:
        return ""
    prompts = cfg.get("groups.prompts") or {}
    if not prompts:
        return ""
    try:
        row = con.execute("SELECT grp FROM channels WHERE channel_id=?",
                          (channel_id,)).fetchone()
    except Exception:
        return ""
    if not row or not row["grp"]:
        return ""
    parts = []
    for g in sorted({x.strip() for x in row["grp"].split(",") if x.strip()}):
        p = (prompts.get(g) or "").strip()
        if p:
            parts.append(f"【组：{g}】{p}")
    return "\n".join(parts)


def generate(cfg, con, video_id: str, can: Canonical,
             video_title: str, channel: str,
             group_prompt: str = "") -> dict:
    chunks = chunk_transcript(can)
    pct = int(cfg.get("article.verbatim_pct", 70) or 0)
    vocab = existing_tag_vocab(con)
    company_vocab = existing_company_vocab(con)
    if pct >= 40:
        sections, ratio = _verbatim_sections(cfg, con, video_id, can, chunks, pct,
                                             group_prompt=group_prompt)
        sample = "\n".join(
            f"## {s['heading']}\n{s['body'][:200]}" for s in sections)[:8000]
        user_msg = f"视频标题：{video_title}\n频道：{channel}\n\n" + sample
        if vocab:
            user_msg += "\n\n已有标签库（打标签时优先复用，不要新造近义词）：\n" + "、".join(vocab)
        if company_vocab:
            user_msg += "\n\n已有公司库（提到同一公司/实体时优先复用同一写法）：\n" + "、".join(company_vocab)
        try:
            reply = providers.complete(cfg, con, video_id, META_SYSTEM,
                                       user_msg, max_tokens=1500, purpose="compose")
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
            "companies": meta.get("companies", []) if isinstance(meta.get("companies"), list) else [],
            "verbatim_pct": pct,
            "verbatim_ratio": ratio,
        }
    else:
        notes = analyze_chunks(cfg, con, video_id, chunks)
        art = compose_article(cfg, con, video_id, video_title, channel, notes,
                              group_prompt=group_prompt, existing_tags=vocab,
                              existing_companies=company_vocab)
    if pct >= 40 and cfg.get("article.punctuation", "ai") == "ai":
        art["punctuated"] = _punctuate_sections(cfg, con, video_id,
                                                art.get("sections", []))
    art["proofread_fixes"] = proofread_sections(cfg, con, video_id,
                                                art.get("sections", []))
    art["_chunks"] = [c.__dict__ | {"text": None} for c in chunks]  # traceability
    art["_mode"] = cfg.get("article.mode", "edited_article")
    return art


def save_article(art: dict, dest_dir: Path) -> Path:
    dest = dest_dir / "article.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(art, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dest)
    return dest
