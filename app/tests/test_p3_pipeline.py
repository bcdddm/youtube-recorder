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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P3 tests passed")
