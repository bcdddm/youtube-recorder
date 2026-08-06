"""Pipeline orchestration for one run (P3 scope: discovery → transcript_ready).

Later phases extend from transcript_ready: article (P5), visuals (P6),
vault write (P7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pathlib import Path

from . import db as dbm
from . import state as st
from . import discovery, probe as probe_mod, download as dl, watchfolder as wf, platforms
from . import transcript as tr
from .paths import work_dir


@dataclass
class RunStats:
    feeds_checked: int = 0
    feeds_not_modified: int = 0
    feed_errors: int = 0
    discovered: int = 0
    ignored: int = 0
    captions_fast_path: int = 0
    queued_for_transcription: int = 0
    submitted_to_watchfolder: int = 0
    transcripts_collected: int = 0
    canonicalized: int = 0
    pending_review: int = 0
    articles_generated: int = 0
    frames_selected: int = 0
    vault_written: int = 0
    failed: int = 0
    dead_lettered: int = 0
    notes: list[str] = field(default_factory=list)
    vault_written_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["notes"] = "; ".join(self.notes) if self.notes else None
        return d



def _set(con, vid: str, status: str, **kw) -> bool:
    """Guarded transition that tolerates user-skip races: if the video's state
    changed underneath us (e.g. user clicked 跳过), give up quietly."""
    try:
        dbm.set_status(con, vid, status, **kw)
        return True
    except st.TransitionError:
        return False


def _fail(con, log, stats, video_id: str, stage: str, kind: str, reason: str) -> None:
    attempt = dbm.bump_attempt(con, video_id)
    max_attempts = st.MAX_ATTEMPTS.get(kind, 0)
    _set(con, video_id, st.FAILED, error_code=reason[:120], retry_class=kind)
    log.event("stage_failed", video_id=video_id, stage=stage,
              error_code=kind, detail=reason[:200], attempt=attempt)
    stats.failed += 1
    if kind == st.RETRY_PERMANENT or (max_attempts and attempt >= max_attempts):
        _set(con, video_id, st.DEAD_LETTER, error_code=reason[:120],
                       retry_class=kind)
        stats.dead_lettered += 1
    elif kind == st.RETRY_TRANSIENT:
        from datetime import datetime, timedelta, timezone
        run_after = (datetime.now(timezone.utc)
                     + timedelta(seconds=st.backoff_seconds(attempt))
                     ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # park it back at the stage it can safely retry from
        _set(con, video_id, stage, run_after=run_after)


def run_discovery(con, cfg, log, stats: RunStats) -> list[str]:
    """Poll all enabled channels. Returns list of NEW video_ids."""
    new_ids: list[str] = []
    for ch in dbm.list_channels(con, enabled_only=True):
        stats.feeds_checked += 1
        if ch["platform"] == "podcast":
            _nb_p = ch["not_before"]
            for it in platforms.fetch_podcast(ch["url"]):
                if _nb_p and it["published"] and it["published"] < _nb_p:
                    continue
                if dbm.upsert_discovered(con, it["video_id"], ch["channel_id"], it["title"], it["published"]):
                    con.execute("UPDATE videos SET platform='podcast', media_url=?, duration_sec=? WHERE video_id=?", (it["media_url"], it["duration_sec"], it["video_id"]))
                    con.commit()
                    new_ids.append(it["video_id"])
                    stats.discovered += 1
                    log.event("video_discovered", video_id=it["video_id"], channel_id=ch["channel_id"], title=(it["title"] or "")[:80])
            continue
        if ch["platform"] == "bilibili":
            for it in platforms.fetch_bilibili(ch["channel_id"]):
                if dbm.upsert_discovered(con, it["video_id"], ch["channel_id"], it["title"], None):
                    con.execute("UPDATE videos SET platform='bilibili' WHERE video_id=?", (it["video_id"],))
                    con.commit()
                    new_ids.append(it["video_id"])
                    stats.discovered += 1
                    log.event("video_discovered", video_id=it["video_id"], channel_id=ch["channel_id"], title=(it["title"] or "")[:80])
            continue
        res = discovery.fetch_feed(ch["channel_id"], ch["feed_etag"],
                                   ch["feed_last_modified"])
        if res.status == "not_modified":
            stats.feeds_not_modified += 1
            continue
        if res.status == "error":
            stats.feed_errors += 1
            log.event("feed_error", channel_id=ch["channel_id"], error_code=res.error)
            continue
        dbm.update_feed_cache(con, ch["channel_id"], res.etag, res.last_modified)
        for e in res.entries:
            if not discovery.accept_entry(e, ch["not_before"]):
                continue
            if dbm.upsert_discovered(con, e.video_id, ch["channel_id"],
                                     e.title, e.published):
                new_ids.append(e.video_id)
                stats.discovered += 1
                log.event("video_discovered", video_id=e.video_id,
                          channel_id=ch["channel_id"], title=e.title[:80])
    return new_ids


def process_discovered(con, cfg, log, stats: RunStats) -> None:
    gate = cfg.get("discovery.review_gate", True)
    batch = dbm.videos_by_status(con, st.DISCOVERED,
                                 limit=cfg.get("discovery.max_new_videos_per_run", 5),
                                 approved_only=gate, oldest_first=True)
    if gate:
        pending = con.execute(
            "SELECT COUNT(*) n FROM videos WHERE status=? AND approved=0",
            (st.DISCOVERED,)).fetchone()["n"]
        if pending:
            stats.pending_review = pending
            log.event("pending_review", detail=f"{pending} videos await approval")
    for v in batch:
        vid = v["video_id"]
        aid = dbm.start_attempt(con, vid, st.METADATA_READY)
        pr = probe_mod.probe(vid, cfg, platform=v["platform"])
        dbm.update_video_meta(con, vid, title=pr.title or None,
                              duration_sec=pr.duration_sec or None,
                              published_at=pr.published_at)
        dbm.update_video_src(con, vid, getattr(pr, "channel_id", None),
                             getattr(pr, "channel_name", None))
        if pr.action in ("permanent", "transient"):
            dbm.end_attempt(con, aid, "error", error_code=pr.action, detail=pr.reason)
            kind = st.RETRY_PERMANENT if pr.action == "permanent" else st.RETRY_TRANSIENT
            _fail(con, log, stats, vid, st.DISCOVERED, kind, pr.reason)
            continue
        _set(con, vid, st.METADATA_READY)
        if pr.action == "ignore":
            _set(con, vid, st.IGNORED, error_code=pr.reason)
            log.event("video_ignored", video_id=vid, detail=pr.reason)
            stats.ignored += 1
            dbm.end_attempt(con, aid, "ok", detail=f"ignored:{pr.reason}")
            continue
        _set(con, vid, st.CAPTION_CHECK)
        if pr.action == "captions":
            dbm.add_artifact(con, vid, "captions_original", pr.caption_file)
            _set(con, vid, st.TRANSCRIPT_READY)
            log.event("captions_fast_path", video_id=vid, detail=pr.caption_lang)
            stats.captions_fast_path += 1
            dbm.end_attempt(con, aid, "ok", detail=f"captions:{pr.caption_lang}")
            continue
        # action == transcribe
        _set(con, vid, st.AUDIO_QUEUED)
        stats.queued_for_transcription += 1
        dbm.end_attempt(con, aid, "ok", detail="no_captions -> audio_queued")
        log.event("caption_unavailable", video_id=vid)


def process_audio_queue(con, cfg, log, stats: RunStats) -> None:
    for v in dbm.videos_by_status(con, st.AUDIO_QUEUED):
        vid = v["video_id"]
        aid = dbm.start_attempt(con, vid, st.AUDIO_QUEUED)
        res = dl.download_audio(vid, platform=v["platform"], media_url=v["media_url"])
        if not res.ok:
            dbm.end_attempt(con, aid, "error", error_code=res.error_kind,
                            detail=res.reason)
            kind = {"permanent": st.RETRY_PERMANENT, "resource": st.RETRY_RESOURCE,
                    "data": st.RETRY_DATA}.get(res.error_kind, st.RETRY_TRANSIENT)
            _fail(con, log, stats, vid, st.AUDIO_QUEUED, kind, res.reason)
            continue
        dbm.add_artifact(con, vid, "audio", str(res.path))
        if cfg.get("transcription.primary") == "openai_audio":
            if _openai_transcribe(con, cfg, log, stats, vid, res.path):
                dbm.end_attempt(con, aid, "ok", detail="openai_audio")
            else:
                dbm.end_attempt(con, aid, "error", error_code="openai_audio")
            continue
        wf.submit_audio(res.path, cfg.inbox_dir)
        _set(con, vid, st.AWAITING_TRANSCRIPTION)
        stats.submitted_to_watchfolder += 1
        dbm.end_attempt(con, aid, "ok")
        log.event("audio_submitted", video_id=vid,
                  detail=str(res.path.stat().st_size))


def collect_transcripts(con, cfg, log, stats: RunStats) -> None:
    timeout = cfg.get("transcription.timeout_minutes", 180)
    for v in dbm.videos_by_status(con, st.AWAITING_TRANSCRIPTION):
        vid = v["video_id"]
        srt = wf.check_srt(vid, cfg.inbox_dir,
                           submitted_at=v["updated_at"], timeout_minutes=timeout)
        if srt.state in ("missing", "unstable"):
            continue  # next run will pick it up
        if srt.state == "timeout":
            # MacWhisper 超时 → 尝试 OpenAI API 兜底（fallback 配置）
            if cfg.get("transcription.fallback") == "openai_audio":
                audio = dbm.get_artifact(con, vid, "audio")
                if audio and Path(audio["path"]).exists():
                    log.event("watchfolder_timeout_fallback", video_id=vid)
                    wf.cleanup_inbox_audio(vid, cfg.inbox_dir)
                    if _openai_transcribe(con, cfg, log, stats, vid,
                                          Path(audio["path"]),
                                          from_stage=st.AWAITING_TRANSCRIPTION):
                        continue
            _fail(con, log, stats, vid, st.AUDIO_QUEUED, st.RETRY_DATA,
                  "watchfolder_timeout")
            continue
        dest = wf.collect_srt(srt.path, work_dir(vid))
        wf.cleanup_inbox_audio(vid, cfg.inbox_dir)
        dbm.add_artifact(con, vid, "srt_original", str(dest))
        _set(con, vid, st.TRANSCRIPT_READY)
        stats.transcripts_collected += 1
        log.event("transcript_collected", video_id=vid, detail=str(dest))


def _openai_transcribe(con, cfg, log, stats, vid: str, audio_path: Path,
                       from_stage: str | None = None) -> bool:
    """直接调 OpenAI API 转译（primary=openai_audio 或 watchfolder 超时兜底）。
    成功 → transcript_ready；失败 → 按错误类型分类。"""
    from . import openai_audio as oa
    v = dbm.get_video(con, vid)
    try:
        dest = oa.transcribe(cfg, con, vid, audio_path,
                             float(v["duration_sec"] or 0) or 1.0,
                             work_dir(vid))
    except oa.OpenAIAudioError as e:
        kind = st.RETRY_TRANSIENT if e.transient else st.RETRY_RESOURCE
        _fail(con, log, stats, vid, from_stage or st.AUDIO_QUEUED, kind, str(e))
        return False
    dbm.add_artifact(con, vid, "srt_original", str(dest))
    _set(con, vid, st.TRANSCRIPT_READY)
    stats.transcripts_collected += 1
    log.event("transcript_openai", video_id=vid, detail=dest.name)
    return True


def canonicalize_transcripts(con, cfg, log, stats: RunStats) -> None:
    """P4: turn collected SRT / caption files into validated canonical JSON."""
    for v in dbm.videos_by_status(con, st.TRANSCRIPT_READY):
        vid = v["video_id"]
        if dbm.get_artifact(con, vid, "transcript_canonical"):
            continue  # already done
        src = dbm.get_artifact(con, vid, "srt_original") \
            or dbm.get_artifact(con, vid, "captions_original")
        if src is None:
            _fail(con, log, stats, vid, st.CAPTION_CHECK, st.RETRY_DATA,
                  "no_transcript_artifact")
            continue
        source_kind = ("macwhisper_srt" if src["kind"] == "srt_original"
                       else "youtube_captions")
        aid = dbm.start_attempt(con, vid, "canonicalize")
        try:
            can = tr.canonicalize(vid, Path(src["path"]),
                                  duration_sec=v["duration_sec"] or 0,
                                  source=source_kind)
        except tr.TranscriptInvalid as e:
            dbm.end_attempt(con, aid, "error", error_code="transcript_invalid",
                            detail=str(e))
            _fail(con, log, stats, vid, st.CAPTION_CHECK, st.RETRY_DATA, str(e))
            continue
        dest = tr.save_canonical(can, work_dir(vid))
        dbm.add_artifact(con, vid, "transcript_canonical", str(dest))
        dbm.end_attempt(con, aid, "ok",
                        detail=f"segments={len(can.segments)} "
                               f"coverage={can.coverage():.2f} "
                               f"warnings={len(can.warnings)}")
        log.event("transcript_canonicalized", video_id=vid,
                  detail=f"{len(can.segments)} segs, "
                         f"cov {can.coverage():.2f}, warn {can.warnings}")
        stats.canonicalized += 1


def process_articles(con, cfg, log, stats: RunStats) -> None:
    """P5: canonical transcript → article JSON (transcript_ready → article_ready)."""
    if not cfg.get("article.enabled", True):
        return
    from . import article as art_mod
    from .providers import ProviderError
    for v in dbm.videos_by_status(con, st.TRANSCRIPT_READY):
        vid = v["video_id"]
        can_art = dbm.get_artifact(con, vid, "transcript_canonical")
        if can_art is None:
            continue  # canonicalizer will handle next round
        if dbm.get_artifact(con, vid, "article_json"):
            _set(con, vid, st.ARTICLE_READY)
            continue
        can = tr.Canonical.from_json(Path(can_art["path"]).read_text(encoding="utf-8"))
        ch = con.execute("SELECT name FROM channels WHERE channel_id=?",
                         (v["channel_id"],)).fetchone()
        aid = dbm.start_attempt(con, vid, "article")
        try:
            art = art_mod.generate(cfg, con, vid, can,
                                   v["title"] or "", ch["name"] if ch else "",
                                   group_prompt=art_mod.group_prompt_for(
                                       cfg, con, v["channel_id"]))
        except ProviderError as e:
            dbm.end_attempt(con, aid, "error", error_code="provider", detail=str(e))
            kind = st.RETRY_TRANSIENT if e.transient else st.RETRY_RESOURCE
            _fail(con, log, stats, vid, st.TRANSCRIPT_READY, kind, str(e))
            continue
        except (ValueError, KeyError) as e:
            dbm.end_attempt(con, aid, "error", error_code="bad_article_json",
                            detail=str(e))
            _fail(con, log, stats, vid, st.TRANSCRIPT_READY, st.RETRY_DATA, str(e))
            continue
        dest = art_mod.save_article(art, work_dir(vid))
        dbm.add_artifact(con, vid, "article_json", str(dest))
        _set(con, vid, st.ARTICLE_READY)
        dbm.end_attempt(con, aid, "ok", detail=art.get("title_zh", "")[:60])
        log.event("article_generated", video_id=vid, detail=art.get("title_zh"))
        stats.articles_generated += 1


def process_visuals(con, cfg, log, stats: RunStats) -> None:
    """P6: article_ready → visual_planned → frames_ready (or straight package
    path via write_vault when disabled / nothing usable)."""
    if not cfg.get("visuals.enabled", True):
        return
    from . import visuals as vz
    from . import article as art_mod
    import json as _json
    density = cfg.get("visuals.image_density", 3)
    for v in dbm.videos_by_status(con, st.ARTICLE_READY):
        vid = v["video_id"]
        wd = work_dir(vid)
        if v["platform"] == "podcast":
            vz.save_plan([], wd)
            _set(con, vid, st.VISUAL_PLANNED)
            _set(con, vid, st.FRAMES_READY)
            continue
        if (wd / "visual-plan.json").exists():
            _set(con, vid, st.VISUAL_PLANNED)
            _set(con, vid, st.FRAMES_READY)
            continue
        can_art = dbm.get_artifact(con, vid, "transcript_canonical")
        art_art = dbm.get_artifact(con, vid, "article_json")
        if not (can_art and art_art):
            continue
        can = tr.Canonical.from_json(Path(can_art["path"]).read_text(encoding="utf-8"))
        art = _json.loads(Path(art_art["path"]).read_text(encoding="utf-8"))
        chunks = art_mod.chunk_transcript(can)
        aid = dbm.start_attempt(con, vid, "visuals")
        cands = vz.recall(can, chunks, density)
        if not cands and density >= 2:
            cands = vz.llm_recall(cfg, con, vid, can, chunks, density)
            if cands:
                log.event("visuals_llm_recall", video_id=vid,
                          detail=f"{len(cands)} candidates")
        if density >= 5:
            spc: dict = {}
            for sec in art.get("sections", []):
                for cid in sec.get("source_chunk_ids") or []:
                    spc[cid] = spc.get(cid, 0) + 1
                    break  # 每节只计首个来源块
            cands = vz.fill_candidates(cands, chunks, spc)
            log.event("visuals_fill_density5", video_id=vid,
                      detail=f"{len(cands)} candidates for {sum(spc.values())} sections")
        _set(con, vid, st.VISUAL_PLANNED)
        if not cands:
            vz.save_plan([], wd)
            _set(con, vid, st.FRAMES_READY)
            dbm.end_attempt(con, aid, "ok", detail="no_candidates")
            log.event("visuals_none", video_id=vid)
            continue
        cands = vz.pick_frames(vid, cands, wd,
                               strict_fill=(density >= 5
                                            or cfg.get("visuals.strict_fill", False)))
        plan = vz.save_plan(cands, wd)
        dbm.add_artifact(con, vid, "visual_plan", str(plan))
        selected = [c for c in cands if c.status == "selected"]
        for c in cands:
            con.execute(
                "INSERT OR REPLACE INTO visuals(video_id,candidate_id,target_ms,"
                "window_start_ms,window_end_ms,reason,selected_frame,"
                "frame_time_ms,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (vid, c.candidate_id, c.target_ms, c.window_ms[0], c.window_ms[1],
                 c.cue, c.selected_frame, c.frame_time_ms, c.status))
        con.commit()
        _set(con, vid, st.FRAMES_READY)
        dbm.end_attempt(con, aid, "ok",
                        detail=f"candidates={len(cands)} selected={len(selected)}")
        log.event("visuals_done", video_id=vid,
                  detail=f"{len(selected)}/{len(cands)} frames")
        stats.frames_selected += len(selected)


def write_vault(con, cfg, log, stats: RunStats) -> None:
    """P7: (article_ready | frames_ready) → package_ready → written → verified."""
    root = cfg.vault_root
    if root is None:
        return  # vault writes disabled until root configured
    if not root.exists():
        log.event("vault_missing", detail=str(root))
        return
    from . import vault as vt
    import json as _json
    raw_sub = cfg.get("vault.raw_subdir", "20-Raw/YouTube")
    wiki_sub = cfg.get("vault.wiki_subdir", "30-Wiki")
    att_sub = cfg.get("vault.attachments_subdir", "40-Attachments/YouTube")
    todo = list(dbm.videos_by_status(con, st.FRAMES_READY))
    if not cfg.get("visuals.enabled", True):
        todo += list(dbm.videos_by_status(con, st.ARTICLE_READY))
    for v in todo:
        vid = v["video_id"]
        can_art = dbm.get_artifact(con, vid, "transcript_canonical")
        art_art = dbm.get_artifact(con, vid, "article_json")
        if not (can_art and art_art):
            continue
        can = tr.Canonical.from_json(Path(can_art["path"]).read_text(encoding="utf-8"))
        art = _json.loads(Path(art_art["path"]).read_text(encoding="utf-8"))
        ch = con.execute("SELECT name FROM channels WHERE channel_id=?",
                         (v["channel_id"],)).fetchone()
        channel = ch["name"] if ch else ""
        url = (v["media_url"] or "") if v["platform"] == "podcast" else platforms.watch_url(v["platform"], vid)
        # copy selected frames into the vault attachments dir
        from . import visuals as vz
        images = []
        for c in vz.load_plan(work_dir(vid)):
            if c.get("status") == "selected" and c.get("selected_frame"):
                src = Path(c["selected_frame"])
                if not src.exists():
                    continue
                att_dir = root / att_sub / vid
                att_dir.mkdir(parents=True, exist_ok=True)
                dst = att_dir / src.name
                if not dst.exists():
                    import shutil as _sh
                    _sh.copyfile(src, dst)
                images.append({"chunk_id": c.get("chunk_id"),
                               "filename": src.name,
                               "time_ms": c.get("frame_time_ms") or c["target_ms"],
                               "cue": c.get("cue", "")})
        layout = cfg.get("vault.layout", "vault")
        aid = dbm.start_attempt(con, vid, "vault_write")
        try:
            _set(con, vid, st.PACKAGE_READY)
            raw_res = None if layout == "folder_flat" else vt.write_raw_note(
                root, raw_sub, video_id=vid, video_title=v["title"] or vid,
                channel=channel, published=v["published_at"] or "",
                video_url=url, can=can)
            raw_name = ("" if layout == "folder_flat" else
                        (raw_res.path.stem if raw_res
                         else _existing_raw_name(root, raw_sub, vid)))
            content = vt.render_wiki_note(
                art, video_id=vid, video_title=v["title"] or vid,
                channel=channel, published=v["published_at"] or "",
                video_url=url, raw_note_name=raw_name or "",
                images=images, attachments_subdir=att_sub,
                original=can if cfg.get("article.append_original", True) else None)
            wiki_res = vt.write_wiki_note(root, wiki_sub, content, vid,
                                          art["title_zh"])
            _set(con, vid, st.WRITTEN)
            ok = wiki_res.readback_ok and (raw_res is None or raw_res.readback_ok)
            for res, kind in ((raw_res, "raw"), (wiki_res, "wiki")):
                if res:
                    con.execute(
                        "INSERT INTO writes(video_id,note_kind,note_path,"
                        "content_hash,readback_ok,at) VALUES(?,?,?,?,?,?)",
                        (vid, kind, str(res.path), res.content_hash,
                         int(res.readback_ok), dbm.now()))
            con.commit()
            if not ok:
                raise vt.VaultError("readback failed")
            _set(con, vid, st.VERIFIED)
            dbm.end_attempt(con, aid, "ok", detail=str(wiki_res.path.name))
            log.event("vault_written", video_id=vid,
                      detail=f"wiki={wiki_res.path.name} "
                             f"raw={'created' if raw_res else 'existing'}")
            stats.vault_written += 1
            stats.vault_written_ids.append(vid)
        except (vt.VaultError, OSError) as e:
            dbm.end_attempt(con, aid, "error", error_code="vault", detail=str(e))
            _fail(con, log, stats, vid, st.ARTICLE_READY, st.RETRY_WRITE, str(e))


def _existing_raw_name(root: Path, raw_sub: str, video_id: str) -> str | None:
    d = root / raw_sub
    if d.exists():
        for p in d.glob(f"*--{video_id}.md"):
            return p.stem
    return None


def confirm_dialog(cfg, log, n_new: int) -> bool:
    """macOS dialog when new videos found. True = proceed."""
    policy = cfg.get("scheduler.confirm_dialog", "on_new_videos")
    if policy == "never" or (policy == "on_new_videos" and n_new == 0):
        return True
    import subprocess
    timeout = cfg.get("scheduler.confirm_timeout_sec", 30)
    script = (
        f'display dialog "发现 {n_new} 条新视频，{timeout} 秒后自动开始处理。" '
        f'with title "YouTube Recorder · By Leoluchino" '
        f'buttons {{"取消", "立即处理"}} default button "立即处理" '
        f'giving up after {timeout}')
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=timeout + 15)
        if "取消" in out.stdout and "gave up:true" not in out.stdout.replace(" ", ""):
            log.event("run_cancelled_by_user", detail=f"{n_new} videos deferred")
            return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return cfg.get("scheduler.on_dialog_error", "run") == "run"


def _downstream(con, cfg, log, stats: RunStats) -> None:
    collect_transcripts(con, cfg, log, stats)
    canonicalize_transcripts(con, cfg, log, stats)
    process_articles(con, cfg, log, stats)
    process_visuals(con, cfg, log, stats)
    write_vault(con, cfg, log, stats)


def run_once(con, cfg, log, *, headless: bool = False) -> RunStats:
    stats = RunStats()
    run_discovery(con, cfg, log, stats)
    if not headless and stats.discovered > 0:
        if not confirm_dialog(cfg, log, stats.discovered):
            stats.notes.append("cancelled_by_user")
            return stats
    process_discovered(con, cfg, log, stats)
    process_audio_queue(con, cfg, log, stats)
    _downstream(con, cfg, log, stats)

    # 关键修复：投给 MacWhisper 后不再直接退出等下一轮——
    # 守在这里轮询回收，出稿立即走完成文→截图→入库。
    import time as _time
    wait_min = cfg.get("transcription.collect_wait_minutes", 45)
    deadline = _time.monotonic() + wait_min * 60
    while (dbm.videos_by_status(con, st.AWAITING_TRANSCRIPTION)
           and _time.monotonic() < deadline):
        log.event("waiting_for_transcription",
                  detail=f"poll every 20s, up to {wait_min}min")
        _time.sleep(20)
        _downstream(con, cfg, log, stats)

    from . import trash
    purged = trash.purge_expired(cfg.get("retention.trash_days", 3))
    if purged:
        log.event("trash_purged", detail=f"{purged} expired entries")

    _trigger_auto_digest(stats, log)
    _trigger_company_dossier(con, cfg, stats, log)
    return stats


def _trigger_auto_digest(stats: RunStats, log) -> None:
    """本轮写入了新文章时，触发后台日报自动生成检查（当天全部组文章数
    超过 2 篇才会真正生成，见 gui.maybe_autogenerate_digest 的阈值/去重逻辑）。
    独立成函数便于单测，不需要跑完整 run_once。"""
    if stats.vault_written <= 0:
        return
    try:
        from . import gui as _gui
        _gui.maybe_autogenerate_digest(log)
    except Exception as e:
        log.event("digest_autogen_hook_failed", detail=str(e))


def _trigger_company_dossier(con, cfg, stats: RunStats, log) -> None:
    """公司档案插件（默认关闭，dossier.enabled）：本轮新写入 vault 的每篇
    文章，检查它 companies 字段里还没处理过的公司，逐个跑一次增量抽取，
    追加进对应公司的档案笔记。独立成函数便于单测。

    同时兜底扫一遍指数/ETF 提及（scan_video_for_index_mentions）——这层
    不依赖 companies 字段，靠固定的别名正则命中标普500/纳斯达克100/
    SOXX/IGV 这几个已登记的指数实体，哪怕某篇文章的 companies 字段没把
    它们标进去（AI 标注难免有漏），只要正文里提到了就不会漏掉。

    dossier.allowed_groups 配置了的话，只处理属于这些组的频道——比如
    只想让投资相关的频道进公司档案，播客/generalist 频道不进。没配置就
    跟以前一样不限制。"""
    if not stats.vault_written_ids or not cfg.get("dossier.enabled", False):
        return
    from . import dossier as _dossier
    for vid in stats.vault_written_ids:
        if not _dossier.video_allowed_for_dossier(cfg, con, vid):
            continue
        try:
            n = _dossier.process_video_companies(cfg, con, vid, log)
            if n:
                log.event("dossier_video_processed", video_id=vid, companies=n)
        except Exception as e:
            log.event("dossier_hook_failed", video_id=vid, detail=str(e))
        try:
            m = _dossier.scan_video_for_index_mentions(cfg, con, vid, log)
            if m:
                log.event("dossier_index_scan_hooked", video_id=vid, indexes=m)
        except Exception as e:
            log.event("dossier_index_scan_hook_failed", video_id=vid, detail=str(e))
