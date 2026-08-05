"""YouTube Recorder CLI. By Leoluchino.

Commands (v0.2 design §13.3):
  ytrec init                      create app dirs, default config, database
  ytrec status                    counts by stage + recent failures
  ytrec channels add URL [--name] register a channel (resolves stable ID)
  ytrec channels list
  ytrec inspect VIDEO_ID          full record: video, artifacts, attempts
  ytrec retry VIDEO_ID --from STAGE
  ytrec run --once                one pipeline pass (discovery lands in P3)
  ytrec dossier-backfill           补跑公司档案插件：过一遍历史文章
"""

from __future__ import annotations

import argparse
import json
import sys

from . import BRANDING, __version__
from . import config as cfg_mod
from . import db as dbm
from . import state as st
from .lock import AlreadyRunning, ProcessLock
from .logging_setup import RunLogger
from .paths import APP_SUPPORT, CONFIG_FILE, DB_FILE, ensure_dirs


def _p(msg: str) -> None:
    print(msg)


# --- commands -----------------------------------------------------------------

def cmd_init(args) -> int:
    ensure_dirs()
    created = cfg_mod.write_default_if_missing()
    con = dbm.connect()
    con.close()
    _p(f"{BRANDING} v{__version__}")
    _p(f"app dir : {APP_SUPPORT}")
    _p(f"config  : {CONFIG_FILE} ({'created' if created else 'exists'})")
    _p(f"database: {DB_FILE} (schema ok)")
    return 0


def cmd_status(args) -> int:
    con = dbm.connect()
    counts = dbm.counts_by_status(con)
    _p(f"{BRANDING} — status")
    if not counts:
        _p("no videos tracked yet")
    for stage in st.ALL_STAGES:
        if stage in counts:
            _p(f"  {stage:24s} {counts[stage]}")
    fails = con.execute(
        "SELECT video_id, error_code, updated_at FROM videos "
        "WHERE status IN ('failed','dead_letter') ORDER BY updated_at DESC LIMIT 10"
    ).fetchall()
    if fails:
        _p("recent failures:")
        for r in fails:
            _p(f"  {r['video_id']}  {r['error_code'] or '-'}  {r['updated_at']}")
    con.close()
    return 0


def _resolve_channel_id(url: str) -> tuple[str, str]:
    """Resolve any channel URL form to (channel_id, channel_name) via yt-dlp."""
    try:
        import yt_dlp  # optional dep
    except ImportError:
        raise SystemExit("yt-dlp not installed: pip install yt-dlp")
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True,
                           "playlist_items": "0"}) as y:
        info = y.extract_info(url, download=False)
    cid = info.get("channel_id") or info.get("id")
    if not cid or not cid.startswith("UC"):
        raise SystemExit(f"could not resolve a UC channel id from {url}")
    return cid, info.get("channel") or info.get("title") or ""


def cmd_channels_add(args) -> int:
    cid, cname = _resolve_channel_id(args.url)
    con = dbm.connect()
    dbm.add_channel(con, cid, args.url, args.name or cname,
                    not_before=args.not_before)
    _p(f"added {cid}  {args.name or cname}"
       + (f"  (not_before={args.not_before})" if args.not_before else ""))
    con.close()
    return 0


def cmd_channels_list(args) -> int:
    con = dbm.connect()
    rows = dbm.list_channels(con)
    if not rows:
        _p("no channels registered")
    for r in rows:
        flag = "on " if r["enabled"] else "off"
        _p(f"  [{flag}] {r['channel_id']}  {r['name'] or ''}  ({r['url']})")
    con.close()
    return 0


def cmd_inspect(args) -> int:
    con = dbm.connect()
    v = dbm.get_video(con, args.video_id)
    if v is None:
        _p(f"unknown video {args.video_id}")
        return 1
    out = {"video": dict(v)}
    out["artifacts"] = [dict(r) for r in con.execute(
        "SELECT kind,path,version,sha256,verified_at FROM artifacts WHERE video_id=?",
        (args.video_id,))]
    out["attempts"] = [dict(r) for r in con.execute(
        "SELECT stage,started_at,ended_at,result,error_code FROM attempts "
        "WHERE video_id=? ORDER BY id DESC LIMIT 20", (args.video_id,))]
    _p(json.dumps(out, ensure_ascii=False, indent=2))
    con.close()
    return 0


def cmd_retry(args) -> int:
    con = dbm.connect()
    v = dbm.get_video(con, args.video_id)
    if v is None:
        _p(f"unknown video {args.video_id}")
        return 1
    if v["status"] not in (st.FAILED, st.DEAD_LETTER):
        _p(f"video is {v['status']}, not failed/dead_letter — nothing to retry")
        return 1
    target = args.from_stage
    dbm.set_status(con, args.video_id, target)
    _p(f"{args.video_id}: {v['status']} -> {target}")
    con.close()
    return 0


def cmd_dossier_backfill(args) -> int:
    log = RunLogger()
    cfg = cfg_mod.load()
    con = dbm.connect()
    from . import dossier
    result = dossier.backfill_all(cfg, con, log)
    _p(f"dossier backfill: scanned={result['scanned']} "
       f"videos_with_companies={result['videos_with_companies']} "
       f"companies_processed={result['companies_processed']}")
    con.close()
    return 0


def cmd_run(args) -> int:
    log = RunLogger()
    try:
        with ProcessLock():
            log.event("run_start", once=args.once)
            cfg = cfg_mod.load()
            con = dbm.connect()
            from . import pipeline
            stats = pipeline.run_once(con, cfg, log, headless=args.headless)
            counts = dbm.counts_by_status(con)
            log.summary(**stats.as_dict(),
                        **{f"n_{k}": v for k, v in counts.items()})
            _p(f"run {log.run_id}: "
               f"discovered={stats.discovered} ignored={stats.ignored} "
               f"captions={stats.captions_fast_path} "
               f"queued={stats.queued_for_transcription} "
               f"submitted={stats.submitted_to_watchfolder} "
               f"collected={stats.transcripts_collected} "
               f"canonical={stats.canonicalized} "
               f"articles={stats.articles_generated} "
               f"vault={stats.vault_written} "
               f"failed={stats.failed}")
            con.close()
            RunLogger.prune_old()
            return 0
    except AlreadyRunning as e:
        log.event("run_skipped", reason="already_running")
        _p(str(e))
        return 3
    except cfg_mod.ConfigError as e:
        log.event("run_error", error_code="config_invalid", detail=str(e))
        _p(str(e))
        return 2


# --- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ytrec",
        description=f"{BRANDING} v{__version__}",
    )
    ap.add_argument("--version", action="version",
                    version=f"{BRANDING} v{__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    ch = sub.add_parser("channels").add_subparsers(dest="chcmd", required=True)
    add = ch.add_parser("add")
    add.add_argument("url")
    add.add_argument("--name")
    add.add_argument("--not-before", dest="not_before",
                     help="ISO8601 UTC, e.g. 2026-07-17T00:00:00Z; "
                          "default = now (no historical backfill)")
    add.set_defaults(fn=cmd_channels_add)
    ch.add_parser("list").set_defaults(fn=cmd_channels_list)

    ins = sub.add_parser("inspect")
    ins.add_argument("video_id")
    ins.set_defaults(fn=cmd_inspect)

    rt = sub.add_parser("retry")
    rt.add_argument("video_id")
    rt.add_argument("--from", dest="from_stage", required=True,
                    choices=list(st.ALL_STAGES))
    rt.set_defaults(fn=cmd_retry)

    tr_ = sub.add_parser("tray")
    tr_.set_defaults(fn=lambda a: __import__(
        "youtube_recorder.tray", fromlist=["main"]).main())

    win = sub.add_parser("app")
    win.set_defaults(fn=lambda a: __import__(
        "youtube_recorder.winapp", fromlist=["main"]).main())

    g = sub.add_parser("gui")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(fn=lambda a: __import__(
        "youtube_recorder.gui", fromlist=["main"]).main(
        port=a.port, open_browser=not a.no_browser) or 0)

    dbf = sub.add_parser(
        "dossier-backfill",
        help="补跑公司档案插件：把库里已有的全部历史文章过一遍增量抽取")
    dbf.set_defaults(fn=cmd_dossier_backfill)

    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--headless", action="store_true",
                     help="never show the confirmation dialog")
    run.set_defaults(fn=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv and getattr(sys, "frozen", False) and sys.platform == "darwin":
        # 打包后的 App 被 Finder/`open` 不带参数启动时（比如登录启动项、
        # 双击图标），默认进菜单栏托盘常驻模式；开窗口走 `open --args app`
        # 单独传参，见 tray.py 的 open_win()。
        argv = ["tray"]
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
