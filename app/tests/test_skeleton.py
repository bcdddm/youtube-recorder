"""P2 skeleton smoke tests. Run: python3 -m pytest tests/ -q  (or plain python3 tests/test_skeleton.py)"""

import os
import sys
import tempfile
import threading
from pathlib import Path

# use an isolated app dir BEFORE importing the package
_TMP = tempfile.mkdtemp(prefix="ytrec-test-")
os.environ["YTREC_HOME"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod          # noqa: E402
from youtube_recorder import db as dbm                  # noqa: E402
from youtube_recorder import state as st                # noqa: E402
from youtube_recorder.lock import AlreadyRunning, ProcessLock  # noqa: E402
from youtube_recorder.logging_setup import RunLogger    # noqa: E402
from youtube_recorder.paths import ensure_dirs          # noqa: E402


def test_config_roundtrip():
    ensure_dirs()
    assert cfg_mod.write_default_if_missing() is True
    cfg = cfg_mod.load()
    assert cfg.get("article.mode") == "edited_article"
    assert cfg.get("scheduler.confirm_dialog") == "on_new_videos"
    cfg.data["visuals"]["image_density"] = 9
    try:
        cfg_mod.save(cfg)
        raise AssertionError("invalid density accepted")
    except cfg_mod.ConfigError:
        pass


def test_state_machine():
    st.guard_transition(st.DISCOVERED, st.METADATA_READY)
    st.guard_transition(st.CAPTION_CHECK, st.AUDIO_QUEUED)
    st.guard_transition(st.FAILED, st.AWAITING_TRANSCRIPTION)  # retry path
    st.guard_transition(st.IGNORED, st.DISCOVERED)  # 用户"取消跳过"重新处理
    for bad in [(st.DISCOVERED, st.WRITTEN), (st.VERIFIED, st.DISCOVERED),
                (st.IGNORED, st.VERIFIED), (st.AUDIO_QUEUED, st.TRANSCRIPT_READY)]:
        try:
            st.guard_transition(*bad)
            raise AssertionError(f"illegal transition allowed: {bad}")
        except st.TransitionError:
            pass
    assert st.backoff_seconds(1) == 60
    assert st.backoff_seconds(5) == 4 * 3600


def test_db_lifecycle():
    con = dbm.connect()
    dbm.add_channel(con, "UCtest123", "https://youtube.com/@t", "Test")
    assert dbm.upsert_discovered(con, "vid001", "UCtest123", "hello", None) is True
    assert dbm.upsert_discovered(con, "vid001", "UCtest123", "hello", None) is False  # idempotent
    dbm.set_status(con, "vid001", st.METADATA_READY)
    dbm.set_status(con, "vid001", st.CAPTION_CHECK)
    dbm.set_status(con, "vid001", st.AUDIO_QUEUED)
    try:
        dbm.set_status(con, "vid001", st.WRITTEN)
        raise AssertionError("illegal transition accepted by db layer")
    except st.TransitionError:
        pass
    dbm.add_artifact(con, "vid001", "audio", "/tmp/vid001.m4a")
    assert dbm.get_artifact(con, "vid001", "audio")["path"] == "/tmp/vid001.m4a"
    aid = dbm.start_attempt(con, "vid001", st.AUDIO_QUEUED)
    dbm.end_attempt(con, aid, "ok")
    assert dbm.counts_by_status(con)[st.AUDIO_QUEUED] == 1
    con.close()


def test_videos_by_status_oldest_first():
    """oldest_first=True 按 created_at 正序（FIFO），默认仍是 published_at
    倒序——process_discovered 用前者避免旧视频被新发现永远挤到批次外。"""
    con = dbm.connect()
    dbm.add_channel(con, "UColdfirst01", "https://youtube.com/@of", "OF")
    dbm.upsert_discovered(con, "of_old", "UColdfirst01", "old", "2026-07-01T00:00:00Z")
    con.execute("UPDATE videos SET created_at=? WHERE video_id=?",
               ("2020-01-01T00:00:00Z", "of_old"))
    dbm.upsert_discovered(con, "of_new", "UColdfirst01", "new", "2026-07-30T00:00:00Z")
    con.commit()

    default_order = dbm.videos_by_status(con, st.DISCOVERED, limit=1)
    assert default_order[0]["video_id"] == "of_new"  # published_at 倒序：新的在前

    fifo_order = dbm.videos_by_status(con, st.DISCOVERED, limit=1, oldest_first=True)
    assert fifo_order[0]["video_id"] == "of_old"  # created_at 正序：旧的在前
    con.close()


def test_lock_mutex():
    results = []
    with ProcessLock():
        def try_second():
            try:
                with ProcessLock():
                    results.append("acquired")
            except AlreadyRunning:
                results.append("blocked")
        t = threading.Thread(target=try_second)
        t.start(); t.join()
    assert results == ["blocked"]
    with ProcessLock():  # released properly
        pass


def test_logger_redaction():
    log = RunLogger()
    log.event("test", detail="using key sk-abcdefghijklmnop and Bearer xyz123token")
    text = log.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text
    assert "xyz123token" not in text
    assert "[REDACTED]" in text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all skeleton tests passed")
