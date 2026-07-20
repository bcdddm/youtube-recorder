"""Obsidian vault writer — governance mode A (user decision 2026-07-19):

- Raw product   → {vault}/20-Raw/YouTube/   immutable, tool only CREATES files
- Wiki product  → {vault}/30-Wiki/          article note, updated by video_id
- videoId is identity: filenames end with --{video_id}; re-runs update the
  existing wiki note (matched by frontmatter youtube_video_id), never duplicate.
- Atomic write (tmp + rename) then read-back verification.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import BRANDING
from .db import now
from .transcript import Canonical

_ILLEGAL = re.compile(r'[/\\:*?"<>|\n\r\t]')
MAX_NAME = 80


class VaultError(RuntimeError):
    pass


def safe_name(s: str, limit: int = MAX_NAME) -> str:
    s = _ILLEGAL.sub(" ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit].rstrip(" .")


def _check_inside(root: Path, p: Path) -> None:
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        raise VaultError(f"path escape blocked: {p}")


def _atomic_write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _readback(path: Path, expected_hash: str) -> bool:
    try:
        data = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return hashlib.sha256(data.encode("utf-8")).hexdigest() == expected_hash


def _fmt_t(ms: int) -> str:
    s = ms // 1000
    return f"{s//60:02d}:{s%60:02d}" if s < 3600 else f"{s//3600}:{s%3600//60:02d}:{s%60:02d}"


def _yaml_list(items) -> str:
    return "[" + ", ".join(f'"{str(i)}"' for i in (items or [])) + "]"


@dataclass
class WriteResult:
    path: Path
    content_hash: str
    readback_ok: bool
    created: bool  # False = updated existing


# --- raw note (immutable) -----------------------------------------------------

def write_raw_note(vault_root: Path, raw_subdir: str, *, video_id: str,
                   video_title: str, channel: str, published: str,
                   video_url: str, can: Canonical) -> WriteResult | None:
    """Create the immutable raw transcript note. Returns None if it already
    exists (mode A: never modify existing raw files)."""
    d = vault_root / raw_subdir
    if d.exists() and any(d.glob(f"*--{video_id}.md")):
        return None  # identity is videoId — never re-create, whatever the title
    date = (published or now())[:10]
    fname = f"{date} {safe_name(video_title, 60)}--{video_id}.md"
    path = d / fname
    _check_inside(vault_root, path)
    body_lines = [f"[{_fmt_t(s.start_ms)}] {s.text}" for s in can.segments]
    content = f"""---
type: raw-transcript
source: youtube
youtube_video_id: {video_id}
channel: "{safe_name(channel)}"
video_title: "{safe_name(video_title)}"
video_url: {video_url}
published: {published or ""}
captured: {now()}
transcript_source: {can.source}
language: {can.language}
segments: {len(can.segments)}
coverage: {round(can.coverage(), 3)}
generator: "{BRANDING}"
---

# {safe_name(video_title)}（原始文稿）

> 本文件为自动采集的原始 transcript，不可修改。整理稿见对应 Wiki 笔记。
> 来源：{video_url}

{chr(10).join(body_lines)}
"""
    h = _atomic_write(path, content)
    return WriteResult(path, h, _readback(path, h), created=True)


# --- wiki note (article, updatable by video_id) --------------------------------

def find_wiki_note(vault_root: Path, wiki_subdir: str, video_id: str) -> Path | None:
    d = vault_root / wiki_subdir
    if not d.exists():
        return None
    for p in d.glob("*.md"):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            continue
        if f"youtube_video_id: {video_id}" in head:
            return p
    return None


def render_wiki_note(art: dict, *, video_id: str, video_title: str, channel: str,
                     published: str, video_url: str, raw_note_name: str,
                     images: list[dict] | None = None,
                     attachments_subdir: str = "40-Attachments/YouTube",
                     original: "Canonical | None" = None) -> str:
    """images: [{chunk_id, filename, time_ms, cue}] — embedded after the section
    whose source_chunk_ids contain the image's chunk_id."""
    secs = []
    chunks = {c["chunk_id"]: c for c in art.get("_chunks", [])}
    img_by_chunk: dict[int, list[dict]] = {}
    for im in images or []:
        img_by_chunk.setdefault(im.get("chunk_id"), []).append(im)
    used_imgs: set[str] = set()
    for s in art["sections"]:
        anchor = ""
        ids = s.get("source_chunk_ids") or []
        if ids and ids[0] in chunks:
            t0 = chunks[ids[0]]["start_ms"]
            sep = "&" if "?" in video_url else "?"
            anchor = (f"\n\n> 来源片段 {_fmt_t(t0)} · "
                      f"[跳到原视频]({video_url}{sep}t={t0//1000})")
        img_md = ""
        for cid in ids:
            for im in img_by_chunk.get(cid, []):
                if im["filename"] in used_imgs:
                    continue
                used_imgs.add(im["filename"])
                sep = "&" if "?" in video_url else "?"
                img_md += (
                    f"\n\n![[{attachments_subdir}/{video_id}/{im['filename']}]]\n"
                    f"> 视频 `{_fmt_t(im['time_ms'])}`：{im.get('cue','画面证据')} · "
                    f"[跳到此处]({video_url}{sep}t={im['time_ms']//1000})")
        secs.append(f"### {s['heading']}\n\n{s['body']}{img_md}{anchor}")
    takeaways = "\n".join(f"- {t}" for t in art.get("takeaways", []))
    return f"""---
type: video
title: "{safe_name(art['title_zh'])}"
aliases: {_yaml_list(art.get('aliases'))}
created: {now()[:10]}
updated: {now()[:10]}
status: auto-draft
tags: {_yaml_list(art.get('tags'))}
youtube_video_id: {video_id}
channel: "{safe_name(channel)}"
published: {published or ""}
sources:
  - type: youtube
    url: {video_url}
    title: "{safe_name(video_title)}"
mode: {art.get('_mode', 'edited_article')}
generator: "{BRANDING}"
---

# {art['title_zh']}

> {art['one_sentence']}

## 摘要

{art['summary']}

## 正文

{chr(10).join(secs)}

## 关键 Takeaways

{takeaways}

## 原始材料

{("- 原始文稿：[[" + raw_note_name + "]]" + chr(10)) if raw_note_name else ""}- 视频：{video_url}
{_render_folded_original(original)}"""


def _render_folded_original(can: "Canonical | None") -> str:
    """AI 改写在前，原文以 Obsidian 可折叠 callout（[!quote]-）附在最后。
    阅读模式下默认收起，点击展开。"""
    if can is None:
        return ""
    lines = "\n".join(f"> [{_fmt_t(s.start_ms)}] {s.text}" for s in can.segments)
    return f"""
## 原文（完整文稿）

> [!quote]- 📜 原始文稿（{len(can.segments)} 段 · 点击展开/收起）
{lines}
"""


def migrate_wiki(con, vault_root: Path, old_sub: str, new_sub: str) -> int:
    """把现有 wiki 文章从旧子目录移动到新子目录，并同步 writes 表路径。"""
    if old_sub == new_sub:
        return 0
    new_dir = vault_root / new_sub
    _check_inside(vault_root, new_dir)
    new_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    rows = con.execute(
        "SELECT id, note_path FROM writes WHERE note_kind='wiki'").fetchall()
    for r in rows:
        p = Path(r["note_path"])
        try:
            p.resolve().relative_to((vault_root / old_sub).resolve())
        except ValueError:
            continue  # 不在旧目录内，跳过
        if not p.exists():
            continue
        dest = new_dir / p.name
        if dest.exists():
            continue
        import shutil as _sh
        _sh.move(str(p), str(dest))
        con.execute("UPDATE writes SET note_path=? WHERE id=?",
                    (str(dest), r["id"]))
        moved += 1
    con.commit()
    return moved


def write_wiki_note(vault_root: Path, wiki_subdir: str, content: str,
                    video_id: str, title_zh: str) -> WriteResult:
    existing = find_wiki_note(vault_root, wiki_subdir, video_id)
    if existing:
        path = existing
        created = False
    else:
        path = vault_root / wiki_subdir / f"{safe_name(title_zh, 60)}--{video_id}.md"
        created = True
    _check_inside(vault_root, path)
    h = _atomic_write(path, content)
    return WriteResult(path, h, _readback(path, h), created=created)
