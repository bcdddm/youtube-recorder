"""Soft-delete for generated articles: move wiki note + attachments into the
app trash, keep 3 days for restore, then purge for real.

The immutable raw transcript note in 20-Raw is NOT touched — it is原料 (mode A).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .paths import APP_SUPPORT
from .db import now

TRASH_DIR = APP_SUPPORT / "trash"
DEFAULT_KEEP_DAYS = 3


def trash_article(con, cfg, video_id: str) -> bool:
    """Move the wiki note and attachments dir to trash; drop writes rows so the
    article leaves the library. Returns False if nothing to delete."""
    root = cfg.vault_root
    if root is None:
        return False
    rows = con.execute(
        "SELECT * FROM writes WHERE video_id=? AND note_kind='wiki'",
        (video_id,)).fetchall()
    if not rows:
        return False
    entry = TRASH_DIR / f"{video_id}-{int(time.time())}"
    entry.mkdir(parents=True, exist_ok=True)
    moved = []
    wiki = Path(rows[-1]["note_path"])
    if wiki.exists():
        shutil.move(str(wiki), entry / wiki.name)
        moved.append({"kind": "wiki", "orig": str(wiki), "name": wiki.name})
    att = root / cfg.get("vault.attachments_subdir", "40-Attachments/YouTube") / video_id
    if att.exists():
        shutil.move(str(att), entry / "attachments")
        moved.append({"kind": "attachments", "orig": str(att),
                      "name": "attachments"})
    meta = {
        "video_id": video_id,
        "deleted_at": now(),
        "title": wiki.stem.rsplit("--", 1)[0],
        "moved": moved,
        "writes_rows": [dict(r) for r in rows],
    }
    (entry / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    con.execute("DELETE FROM writes WHERE video_id=? AND note_kind='wiki'",
                (video_id,))
    con.commit()
    return True


def list_trash(keep_days: int = DEFAULT_KEEP_DAYS) -> list[dict]:
    out = []
    if not TRASH_DIR.exists():
        return out
    for entry in sorted(TRASH_DIR.iterdir()):
        meta_p = entry / "meta.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        age_days = (time.time() - entry.stat().st_mtime) / 86400
        meta["entry"] = entry.name
        meta["days_left"] = max(0, round(keep_days - age_days, 1))
        out.append(meta)
    return out


def restore(con, entry_name: str) -> bool:
    entry = TRASH_DIR / entry_name
    meta_p = entry / "meta.json"
    if not meta_p.exists() or "/" in entry_name or ".." in entry_name:
        return False
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    for m in meta["moved"]:
        src = entry / m["name"]
        dst = Path(m["orig"])
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    for r in meta.get("writes_rows", []):
        con.execute(
            "INSERT INTO writes(video_id,note_kind,note_path,content_hash,"
            "readback_ok,at) VALUES(?,?,?,?,?,?)",
            (r["video_id"], r["note_kind"], r["note_path"],
             r.get("content_hash"), r.get("readback_ok"), r["at"]))
    con.commit()
    shutil.rmtree(entry, ignore_errors=True)
    return True


def purge_expired(keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """Really delete trash entries older than keep_days. Returns count purged."""
    purged = 0
    if not TRASH_DIR.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    for entry in TRASH_DIR.iterdir():
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            purged += 1
    return purged
