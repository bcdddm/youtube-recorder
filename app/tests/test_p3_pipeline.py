"""P3 tests: RSS parsing, not_before filter, pipeline state flow with mocked
probe/download/watchfolder (no network)."""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytrec-p3-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod      # noqa: E402
from youtube_recorder import db as dbm              # noqa: E402
from youtube_recorder import discovery              # noqa: E402
from youtube_recorder import pipeline               # noqa: E402
from youtube_recorder import state as st            # noqa: E402
from youtube_recorder.logging_setup import RunLogger  # noqa: E402
from youtube_recorder.paths import ensure_dirs      # noqa: E402
from youtube_recorder.probe import ProbeResult      # noqa: E402
from youtube_recorder.download import DownloadResult  # noqa: E402
from youtube_recorder.watchfolder import SrtStatus  # noqa: E402

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>newvid00001</yt:videoId>
  <title>Fresh video</title>
  <published>2026-07-18T10:00:00+00:00</published>
 </entry>
 <entry>
  <yt:videoId>oldvid00001</yt:videoId>
  <title>Old video</title>
  <published>2026-07-01T10:00:00+00:00</published>
 </entry>
</feed>"""


def test_feed_parse_and_not_before():
    entries = discovery.parse_feed(FEED_XML)
    assert [e.video_id for e in entries] == ["newvid00001", "oldvid00001"]
    nb = "2026-07-10T00:00:00Z"
    kept = [e for e in entries if discovery.accept_entry(e, nb)]
    assert [e.video_id for e in kept] == ["newvid00001"]
    assert discovery.accept_entry(entries[1], None) is True


def _setup(monkey_feed=True):
    ensure_dirs()
    cfg_mod.write_default_if_missing()
    cfg = cfg_mod.load()
    cfg.data["transcription"]["inbox_dir"] = str(Path(_TMP) / "inbox")
    # 这份测试文件测的是 watchfolder/MacWhisper 那条路径（提交音频到 inbox、
    # 手动放一个 .srt 模拟被转录完），默认转录方式现在按平台走（Windows/
    # Linux 默认 openai_audio，见 config.py 的 _default_transcriber）——
    # 显式钉死在这，测试意图跟宿主机平台脱钩，在 CI 的 Windows runner 上
    # 跑这份文件也是同一个结果。
    cfg.data["transcription"]["primary"] = "macwhisper_watch_srt"
    con = dbm.connect()
    dbm.add_channel(con, "UCchan00001", "https://youtube.com/@x", "X",
                    not_before="2026-07-10T00:00:00Z")
    log = RunLogger()
    return con, cfg, log


def test_pipeline_full_flow():
    con, cfg, log = _setup()
    stats = pipeline.RunStats()

    with mock.patch.object(discovery, "fetch_feed",
                           return_value=discovery.FeedResult(
                               status="ok",
                               entries=discovery.parse_feed(FEED_XML),
                               etag="W/\"abc\"")):
        new = pipeline.run_discovery(con, cfg, log, stats)
    assert new == ["newvid00001"]          # old one filtered by not_before
    assert stats.discovered == 1
    # idempotent second pass
    with mock.patch.object(discovery, "fetch_feed",
                           return_value=discovery.FeedResult(
                               status="ok",
                               entries=discovery.parse_feed(FEED_XML))):
        again = pipeline.run_discovery(con, cfg, log, stats)
    assert again == []

    # probe says: no captions -> transcribe
    with mock.patch.object(pipeline.probe_mod, "probe",
                           return_value=ProbeResult(
                               "transcribe", title="Fresh video",
                               duration_sec=1078,
                               published_at="2026-07-18T00:00:00Z")):
        pipeline.process_discovered(con, cfg, log, stats)
    assert dbm.get_video(con, "newvid00001")["status"] == st.AUDIO_QUEUED

    # download ok -> submitted to watch folder
    fake_audio = Path(_TMP) / "newvid00001.m4a"
    fake_audio.write_bytes(b"x" * 1024)
    with mock.patch.object(pipeline.dl, "download_audio",
                           return_value=DownloadResult(True, path=fake_audio)):
        pipeline.process_audio_queue(con, cfg, log, stats)
    v = dbm.get_video(con, "newvid00001")
    assert v["status"] == st.AWAITING_TRANSCRIPTION
    assert (Path(_TMP) / "inbox" / "newvid00001.m4a").exists()

    # srt not there yet -> stays awaiting
    with mock.patch.object(pipeline.wf, "check_srt",
                           return_value=SrtStatus("missing")):
        pipeline.collect_transcripts(con, cfg, log, stats)
    assert dbm.get_video(con, "newvid00001")["status"] == st.AWAITING_TRANSCRIPTION

    # srt ready -> collected, transcript_ready
    srt = Path(_TMP) / "inbox" / "newvid00001.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n\n", encoding="utf-8")
    pipeline.collect_transcripts(con, cfg, log, stats)
    v = dbm.get_video(con, "newvid00001")
    assert v["status"] == st.TRANSCRIPT_READY
    art = dbm.get_artifact(con, "newvid00001", "srt_original")
    assert art and Path(art["path"]).exists()
    assert not srt.exists()                                    # moved out
    assert not (Path(_TMP) / "inbox" / "newvid00001.m4a").exists()  # cleaned
    assert stats.transcripts_collected == 1
    con.close()


def test_process_discovered_fifo_prevents_starvation():
    """0.4.19 之后发现的饥饿 bug 报告：一个视频（比如被自动判定 is_live
    跳过、用户又手动"取消跳过"复活的）一直卡在①已发现不推进。根因是
    process_discovered 按 published_at 倒序只取批次上限（默认 5）条，
    源源不断的新发现会把等待更久的旧视频永远挤到后面。
    现在改成按 created_at 正序（先发现先处理），保证等得最久的优先。"""
    con, cfg, log = _setup()
    # cfg 是 config._LOAD_CACHE 缓存的同一个对象（文件 mtime 不变就不会重新
    # 读盘），改完必须还原，否则会漏到后面其它测试的 _setup() 里。
    orig_limit = cfg.data["discovery"]["max_new_videos_per_run"]
    cfg.data["discovery"]["max_new_videos_per_run"] = 1
    stats = pipeline.RunStats()
    try:
        # old_stuck：很早就发现了（created_at 很旧），但 published_at 比较早
        dbm.upsert_discovered(con, "old_stuck", "UCchan00001", "old", "2026-07-01T00:00:00Z")
        con.execute("UPDATE videos SET created_at=? WHERE video_id=?",
                   ("2020-01-01T00:00:00Z", "old_stuck"))
        # new_fresh：刚发现，但 published_at 更新——旧逻辑（published_at 倒序）
        # 会优先选它，把 old_stuck 永远挤到批次之外
        dbm.upsert_discovered(con, "new_fresh", "UCchan00001", "new", "2026-07-30T00:00:00Z")
        con.commit()

        with mock.patch.object(pipeline.probe_mod, "probe",
                               return_value=ProbeResult("ignore", reason="test")):
            pipeline.process_discovered(con, cfg, log, stats)

        assert dbm.get_video(con, "old_stuck")["status"] == st.IGNORED, \
            "等待最久的视频应该优先被这一批处理，而不是被更新的挤掉"
        assert dbm.get_video(con, "new_fresh")["status"] == st.DISCOVERED, \
            "超过批次上限的视频应保留在原状态，等下一轮"
    finally:
        cfg.data["discovery"]["max_new_videos_per_run"] = orig_limit
        con.execute("DELETE FROM videos WHERE video_id IN ('old_stuck','new_fresh')")
        con.commit()
        con.close()


def test_pipeline_ignore_and_permanent():
    con, cfg, log = _setup()
    stats = pipeline.RunStats()
    dbm.upsert_discovered(con, "shortvid001", "UCchan00001", "short", None)
    with mock.patch.object(pipeline.probe_mod, "probe",
                           return_value=ProbeResult("ignore", reason="short:45s",
                                                    duration_sec=45)):
        pipeline.process_discovered(con, cfg, log, stats)
    assert dbm.get_video(con, "shortvid001")["status"] == st.IGNORED

    dbm.upsert_discovered(con, "membervid01", "UCchan00001", "members", None)
    with mock.patch.object(pipeline.probe_mod, "probe",
                           return_value=ProbeResult("permanent",
                                                    reason="members-only")):
        pipeline.process_discovered(con, cfg, log, stats)
    assert dbm.get_video(con, "membervid01")["status"] == st.DEAD_LETTER
    con.close()


def test_pipeline_dispatches_to_openai_audio_and_reaches_transcript_ready():
    """回归测试：写 faster_whisper 那条本地转录路径（见
    test_faster_whisper.py）时才第一次真正端到端跑通 primary=openai_audio
    这条路径，结果发现 state.py 的 TRANSITIONS 里根本没放行
    AUDIO_QUEUED -> TRANSCRIPT_READY 这条边——_openai_transcribe 调用的
    _set(...) 会被 guard_transition 拒绝，又被 _set() 自己的
    try/except TransitionError 悄悄吞掉（两个调用方都没检查 _set 的返回
    值），视频卡在 audio_queued 不再往前走，而且没有任何报错。这条路径
    之前完全没有测试覆盖到，只测过 openai_audio.py 内部的纯函数
    （切段/合并/SRT），从没测过 pipeline.py 怎么调用它。现在 state.py 已经
    把这条边加回去了，这个测试专门钉住 primary=openai_audio 这条路径不再
    回归。"""
    con, cfg, log = _setup()
    cfg.data["transcription"]["primary"] = "openai_audio"
    stats = pipeline.RunStats()

    with mock.patch.object(pipeline.probe_mod, "probe",
                           return_value=ProbeResult(
                               "transcribe", title="oa", duration_sec=90,
                               published_at="2026-07-18T00:00:00Z")):
        dbm.upsert_discovered(con, "vidoa0001", "UCchan00001", "oa", None)
        con.commit()
        pipeline.process_discovered(con, cfg, log, stats)
    assert dbm.get_video(con, "vidoa0001")["status"] == st.AUDIO_QUEUED

    fake_audio = Path(_TMP) / "vidoa0001.m4a"
    fake_audio.write_bytes(b"x" * 1024)
    fake_srt = Path(_TMP) / "vidoa0001.srt"
    fake_srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n\n",
                        encoding="utf-8")

    with mock.patch.object(pipeline.dl, "download_audio",
                           return_value=DownloadResult(True, path=fake_audio)), \
         mock.patch("youtube_recorder.openai_audio.transcribe",
                    return_value=fake_srt) as m:
        pipeline.process_audio_queue(con, cfg, log, stats)

    m.assert_called_once()
    v = dbm.get_video(con, "vidoa0001")
    assert v["status"] == st.TRANSCRIPT_READY
    art = dbm.get_artifact(con, "vidoa0001", "srt_original")
    assert art and art["path"] == str(fake_srt)
    con.close()


def test_trigger_auto_digest_hook():
    """run_once 结尾的自动日报钩子：只有本轮确实写入了新文章
    （stats.vault_written > 0）才会调用 gui.maybe_autogenerate_digest；
    实际的阈值/去重判断在 gui 那边单独测试（test_p9_gui.py）。"""
    con, cfg, log = _setup()
    stats = pipeline.RunStats()
    calls = []
    with mock.patch("youtube_recorder.gui.maybe_autogenerate_digest",
                     side_effect=lambda log=None: calls.append(1)):
        pipeline._trigger_auto_digest(stats, log)
        assert calls == [], "没有新写入不该触发"
        stats.vault_written = 3
        pipeline._trigger_auto_digest(stats, log)
        assert calls == [1], "有新写入应该触发一次"
    con.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P3 tests passed")
