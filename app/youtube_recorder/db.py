"""SQLite job/artifact store (v0.2 design §2.2, §7.2).

Single-machine, no server. WAL mode; all state transitions in short
transactions; files stay on disk, DB stores index/status/paths only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .paths import DB_FILE
from . import state as st

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    not_before TEXT,
    language_hint TEXT,
    permission_basis TEXT DEFAULT 'personal-research',
    feed_etag TEXT,
    feed_last_modified TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    title TEXT,
    published_at TEXT,
    duration_sec INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    retry_class TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    run_after TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_updated ON videos(updated_at DESC);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    kind TEXT NOT NULL,             -- metadata|audio|srt_original|transcript_canonical|article_json|visual_plan|frame|manifest|note_raw|note_wiki
    path TEXT NOT NULL,
    sha256 TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    UNIQUE(video_id, kind, version)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    result TEXT,                    -- ok|error
    error_code TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    units REAL NOT NULL,
    unit_type TEXT NOT NULL,        -- audio_minutes|input_tokens|output_tokens
    estimated_cost_usd REAL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    target_ms INTEGER NOT NULL,
    window_start_ms INTEGER,
    window_end_ms INTEGER,
    reason TEXT,
    selected_frame TEXT,
    frame_time_ms INTEGER,
    score REAL,
    status TEXT NOT NULL DEFAULT 'candidate',   -- candidate|selected|rejected|no_usable_frame
    UNIQUE(video_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    note_kind TEXT NOT NULL,        -- raw|wiki
    note_path TEXT NOT NULL,
    content_hash TEXT,
    readback_ok INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_processed (
    company TEXT NOT NULL,
    video_id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (company, video_id)
);
CREATE TABLE IF NOT EXISTS dossier_entities (
    name TEXT PRIMARY KEY,          -- 原始名字，可能本身就是 canonical
    canonical TEXT,                 -- 非空=这是个别名，指向真正的 canonical name
    category TEXT NOT NULL DEFAULT 'entity',  -- entity | index_etf
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    ticker TEXT,                    -- 雅虎财经代码缓存：NULL=没查过，
                                     -- ''=查过但确认没有，'XXXX'=真代码
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_price_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,          -- canonical name
    video_id TEXT NOT NULL,
    channel TEXT,
    mentioned_date TEXT,            -- 视频发布日期 YYYY-MM-DD
    level_type TEXT,                -- support | resistance | target | stop_loss | entry | exit | other
    price REAL,
    raw_text TEXT NOT NULL,
    source_link TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dossier_price_levels_company
    ON dossier_price_levels(company);
CREATE INDEX IF NOT EXISTS idx_attempts_video ON attempts(video_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_writes_video ON writes(video_id, note_kind);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse a stored/feed ISO8601 timestamp into an aware datetime.

    Handles both our own UTC 'Z' stamps (see now()) and YouTube feed values
    that carry an explicit offset (e.g. '2026-07-21T05:30:00+00:00'). A bare
    value with no offset is assumed to be UTC.
    """
    if not ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_date(ts: str | None) -> str:
    """UTC-stored timestamp -> local calendar date 'YYYY-MM-DD'.

    Timestamps are stored in UTC for stable sorting, but the user thinks in
    their own timezone. Converting on display keeps a video processed at, say,
    09:00 local (which is the previous day in UTC) on the correct local day.
    Falls back to a naive slice if the value can't be parsed.
    """
    dt = _parse_iso(ts)
    return dt.astimezone().strftime("%Y-%m-%d") if dt else (ts or "")[:10]


def local_time(ts: str | None) -> str:
    """UTC-stored timestamp -> local wall-clock time 'HH:MM:SS'."""
    dt = _parse_iso(ts)
    return dt.astimezone().strftime("%H:%M:%S") if dt else (ts or "")[11:19]


def connect(path: Path = DB_FILE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")  # WAL 下安全，写入显著加快
    con.execute("PRAGMA foreign_keys=ON")
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    cur = con.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),))
        con.commit()
    # v2: review-gate column (approve before processing)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(videos)")}
    if "approved" not in cols:
        con.execute("ALTER TABLE videos ADD COLUMN approved INTEGER NOT NULL DEFAULT 0")
        # existing rows were processed under the old flow — mark approved
        con.execute("UPDATE videos SET approved=1 WHERE status!='discovered'")
        con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        con.commit()
    # v3: 手动添加视频记录真实来源频道（用于订阅 Suggestion）
    if "src_channel_id" not in cols:
        con.execute("ALTER TABLE videos ADD COLUMN src_channel_id TEXT")
        con.execute("ALTER TABLE videos ADD COLUMN src_channel_name TEXT")
        con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        con.commit()
    # v4: 频道分组
    ccols = {r["name"] for r in con.execute("PRAGMA table_info(channels)")}
    if "grp" not in ccols:
        con.execute("ALTER TABLE channels ADD COLUMN grp TEXT DEFAULT ''")
        con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        con.commit()


    # v5: platform 字段（youtube/bilibili/podcast）
    if "platform" not in ccols:
        con.execute("ALTER TABLE channels ADD COLUMN platform TEXT NOT NULL DEFAULT 'youtube'")
        con.execute("ALTER TABLE videos ADD COLUMN platform TEXT NOT NULL DEFAULT 'youtube'")
        con.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        con.commit()


    # v6: media_url（播客音频直链）
    vcols = {r["name"] for r in con.execute("PRAGMA table_info(videos)")}
    if "media_url" not in vcols:
        con.execute("ALTER TABLE videos ADD COLUMN media_url TEXT")
        con.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        con.commit()


# --- channels ----------------------------------------------------------------

def add_channel(con, channel_id: str, url: str, name: str | None = None,
                not_before: str | None = None) -> None:
    con.execute(
        "INSERT OR IGNORE INTO channels(channel_id,url,name,added_at,not_before) "
        "VALUES(?,?,?,?,?)",
        (channel_id, url, name, now(), not_before or now()),
    )
    con.commit()


def list_channels(con, enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM channels"
    if enabled_only:
        q += " WHERE enabled=1"
    return con.execute(q + " ORDER BY added_at").fetchall()


def update_feed_cache(con, channel_id: str, etag: str | None,
                      last_modified: str | None) -> None:
    con.execute(
        "UPDATE channels SET feed_etag=?, feed_last_modified=? WHERE channel_id=?",
        (etag, last_modified, channel_id),
    )
    con.commit()


# --- videos ------------------------------------------------------------------

def upsert_discovered(con, video_id: str, channel_id: str, title: str,
                      published_at: str | None) -> bool:
    """Insert a newly discovered video. Returns True if it was new."""
    cur = con.execute(
        "INSERT OR IGNORE INTO videos(video_id,channel_id,title,published_at,"
        "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (video_id, channel_id, title, published_at, st.DISCOVERED, now(), now()),
    )
    con.commit()
    return cur.rowcount == 1


def get_video(con, video_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()


def set_status(con, video_id: str, new_status: str, *,
               error_code: str | None = None, retry_class: str | None = None,
               run_after: str | None = None) -> None:
    """Guarded status transition inside a single transaction."""
    with con:
        row = con.execute(
            "SELECT status FROM videos WHERE video_id=?", (video_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown video {video_id}")
        st.guard_transition(row["status"], new_status)
        con.execute(
            "UPDATE videos SET status=?, error_code=?, retry_class=?, "
            "run_after=?, updated_at=? WHERE video_id=?",
            (new_status, error_code, retry_class, run_after, now(), video_id),
        )


def videos_by_status(con, status: str, limit: int = 100,
                     approved_only: bool = False,
                     oldest_first: bool = False) -> list[sqlite3.Row]:
    """oldest_first=True 按 created_at 正序（先发现先处理，FIFO）——用于有
    per-run 数量上限的批次查询，避免旧的（含手动"取消跳过"复活的）视频被
    源源不断的新发现挤到后面、永远排不上号（0.4.19 之后发现的饥饿问题）。
    默认仍是 published_at 倒序，不影响其它无上限批次调用方。"""
    q = ("SELECT * FROM videos WHERE status=? AND (run_after IS NULL OR run_after<=?)")
    if approved_only:
        q += " AND approved=1"
    order = "created_at ASC" if oldest_first else "published_at DESC"
    return con.execute(q + f" ORDER BY {order} LIMIT ?",
                       (status, now(), limit)).fetchall()


def approve_video(con, video_id: str) -> None:
    con.execute("UPDATE videos SET approved=1, updated_at=? WHERE video_id=?",
                (now(), video_id))
    con.commit()


def update_video_meta(con, video_id: str, *, title: str | None = None,
                      duration_sec: int | None = None,
                      published_at: str | None = None) -> None:
    con.execute(
        "UPDATE videos SET title=COALESCE(?,title), "
        "duration_sec=COALESCE(?,duration_sec), "
        "published_at=COALESCE(?,published_at), updated_at=? WHERE video_id=?",
        (title, duration_sec, published_at, now(), video_id),
    )
    con.commit()


def update_video_src(con, video_id: str, src_id: str | None,
                     src_name: str | None) -> None:
    if src_id:
        con.execute("UPDATE videos SET src_channel_id=?, src_channel_name=? "
                    "WHERE video_id=?", (src_id, src_name, video_id))
        con.commit()


def bump_attempt(con, video_id: str) -> int:
    with con:
        con.execute("UPDATE videos SET attempt=attempt+1 WHERE video_id=?", (video_id,))
        return con.execute("SELECT attempt FROM videos WHERE video_id=?",
                           (video_id,)).fetchone()["attempt"]


def counts_by_status(con) -> dict[str, int]:
    rows = con.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# --- artifacts ---------------------------------------------------------------

def add_artifact(con, video_id: str, kind: str, path: str,
                 sha256: str | None = None, version: int = 1) -> None:
    con.execute(
        "INSERT OR REPLACE INTO artifacts(video_id,kind,path,sha256,version,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (video_id, kind, path, sha256, version, now()),
    )
    con.commit()


def get_artifact(con, video_id: str, kind: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM artifacts WHERE video_id=? AND kind=? ORDER BY version DESC LIMIT 1",
        (video_id, kind),
    ).fetchone()


# --- attempts ----------------------------------------------------------------

def start_attempt(con, video_id: str, stage: str) -> int:
    cur = con.execute(
        "INSERT INTO attempts(video_id,stage,started_at) VALUES(?,?,?)",
        (video_id, stage, now()),
    )
    con.commit()
    return cur.lastrowid


def end_attempt(con, attempt_id: int, result: str,
                error_code: str | None = None, detail: str | None = None) -> None:
    con.execute(
        "UPDATE attempts SET ended_at=?, result=?, error_code=?, detail=? WHERE id=?",
        (now(), result, error_code, detail, attempt_id),
    )
    con.commit()


# --- 公司档案增量抽取状态（哪些 (公司, 文章) 组合已经处理过） -----------------

def dossier_unprocessed(con, video_id: str, companies: list[str]) -> list[str]:
    """给定一篇文章提到的公司列表，返回其中还没为这篇文章跑过抽取的那些。"""
    if not companies:
        return []
    rows = con.execute(
        "SELECT company FROM dossier_processed WHERE video_id=?", (video_id,)
    ).fetchall()
    done = {r["company"] for r in rows}
    return [c for c in companies if c not in done]


def dossier_mark_processed(con, video_id: str, company: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO dossier_processed(company, video_id, at) VALUES (?,?,?)",
        (company, video_id, now()))
    con.commit()


# --- dossier_entities：登记表（别名解析 + 待批准队列） -----------------------

def dossier_get_entity(con, name: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM dossier_entities WHERE name=?", (name,)).fetchone()


def dossier_register_entity(con, name: str, *, category: str = "entity",
                            status: str = "pending", canonical: str | None = None
                            ) -> sqlite3.Row:
    """登记一个原始名字（如果已经登记过，只刷新 last_seen，不改已有状态）。
    返回登记后的行。"""
    row = dossier_get_entity(con, name)
    ts = now()
    if row is None:
        con.execute(
            "INSERT INTO dossier_entities(name, canonical, category, status, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (name, canonical, category, status, ts, ts))
        con.commit()
        return dossier_get_entity(con, name)
    con.execute("UPDATE dossier_entities SET last_seen=? WHERE name=?", (ts, name))
    con.commit()
    return dossier_get_entity(con, name)


def dossier_set_entity_status(con, name: str, status: str) -> None:
    con.execute("UPDATE dossier_entities SET status=? WHERE name=?", (status, name))
    con.commit()


def dossier_set_entity_alias(con, name: str, canonical: str) -> None:
    """把 name 登记为 canonical 的别名（合并时用）。"""
    row = dossier_get_entity(con, name)
    ts = now()
    if row is None:
        con.execute(
            "INSERT INTO dossier_entities(name, canonical, category, status, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (name, canonical, "entity", "approved", ts, ts))
    else:
        con.execute("UPDATE dossier_entities SET canonical=? WHERE name=?",
                    (canonical, name))
    con.commit()


def dossier_resolve_entity(con, name: str) -> dict:
    """给一个原始名字，返回 {canonical, status, category, is_new}。没登记过
    的名字会以 status='pending' 自动登记（自己就是 canonical）。"""
    row = dossier_get_entity(con, name)
    if row is None:
        dossier_register_entity(con, name)
        return {"canonical": name, "status": "pending", "category": "entity",
                "is_new": True}
    con.execute("UPDATE dossier_entities SET last_seen=? WHERE name=?",
               (now(), name))
    con.commit()
    if row["canonical"]:
        target = dossier_get_entity(con, row["canonical"])
        status = target["status"] if target else row["status"]
        category = target["category"] if target else row["category"]
        canonical = row["canonical"]
    else:
        status, category, canonical = row["status"], row["category"], row["name"]
    return {"canonical": canonical, "status": status, "category": category,
           "is_new": False}


def dossier_pending_entities(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM dossier_entities WHERE status='pending' AND canonical IS NULL "
        "ORDER BY last_seen DESC").fetchall()


# --- dossier_price_levels：结构化推荐点位 -----------------------------------

def dossier_add_price_level(con, *, company: str, video_id: str, channel: str | None,
                            mentioned_date: str | None, level_type: str | None,
                            price: float | None, raw_text: str,
                            source_link: str | None) -> None:
    con.execute(
        "INSERT INTO dossier_price_levels(company, video_id, channel, mentioned_date, "
        "level_type, price, raw_text, source_link, at) VALUES (?,?,?,?,?,?,?,?,?)",
        (company, video_id, channel, mentioned_date, level_type, price, raw_text,
         source_link, now()))
    con.commit()


def dossier_price_levels_for(con, company: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM dossier_price_levels WHERE company=? "
        "ORDER BY mentioned_date ASC, id ASC", (company,)).fetchall()
