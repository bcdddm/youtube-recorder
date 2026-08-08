"""faster_whisper_backend.py 适配器 + pipeline.py 里 primary=faster_whisper
分支的测试。

faster-whisper 本身是个几百 MB 的重依赖（底层 CTranslate2 + 模型权重），
不该要求本地/CI 环境都装它才能跑测试——用 sys.modules 注入一个假的
faster_whisper 模块（跟 test_creds.py 里假 keyring 模块同一套手法），
只测试这个项目自己写的适配层逻辑（参数传递、SRT 写出、成本记录、
错误分类），不测 faster-whisper 库本身的转录准确率。"""

import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytrec-fw-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod           # noqa: E402
from youtube_recorder import db as dbm                   # noqa: E402
from youtube_recorder import faster_whisper_backend as fw  # noqa: E402
from youtube_recorder import pipeline                     # noqa: E402
from youtube_recorder import state as st                  # noqa: E402
from youtube_recorder.logging_setup import RunLogger      # noqa: E402
from youtube_recorder.download import DownloadResult      # noqa: E402
from youtube_recorder.probe import ProbeResult            # noqa: E402


class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeModel:
    calls = []  # 记录每次实例化时传的参数，验证配置真的传下去了

    def __init__(self, model_size, device=None, compute_type=None):
        _FakeModel.calls.append((model_size, device, compute_type))

    def transcribe(self, path, language=None, vad_filter=True):
        segs = [_FakeSeg(0.0, 2.0, "hello"), _FakeSeg(2.0, 4.5, "world")]
        return iter(segs), types.SimpleNamespace(language="en")


class _EmptyModel:
    def __init__(self, *a, **k):
        pass

    def transcribe(self, path, language=None, vad_filter=True):
        return iter([]), types.SimpleNamespace(language="en")


def _fake_pkg(model_cls):
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = model_cls
    return mod


def _cfg():
    return cfg_mod.Config(dict(cfg_mod.DEFAULT_CONFIG))


def test_transcriber_registered_as_valid_and_has_config_defaults():
    assert "faster_whisper" in cfg_mod.VALID_TRANSCRIBERS
    d = cfg_mod.DEFAULT_CONFIG["transcription"]
    assert d["local_model"] == "small"
    assert d["local_device"] == "cpu"
    assert d["local_compute_type"] == "int8"


def test_missing_package_raises_permanent_error(monkeypatch):
    fw._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    audio = Path(_TMP) / "a1.m4a"
    audio.write_bytes(b"x" * 100)
    work = Path(_TMP) / "work1"
    work.mkdir(exist_ok=True)
    try:
        fw.transcribe(_cfg(), None, "vid1", audio, 60.0, work)
        raise AssertionError("should have raised")
    except fw.FasterWhisperError as e:
        assert e.transient is False
        assert "not installed" in str(e)


def test_empty_transcription_raises_permanent_error(monkeypatch):
    fw._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_pkg(_EmptyModel))
    audio = Path(_TMP) / "a2.m4a"
    audio.write_bytes(b"x" * 100)
    work = Path(_TMP) / "work2"
    work.mkdir(exist_ok=True)
    try:
        fw.transcribe(_cfg(), None, "vid2", audio, 60.0, work)
        raise AssertionError("should have raised")
    except fw.FasterWhisperError as e:
        assert e.transient is False
        assert "empty" in str(e)


def test_transcribe_writes_srt_and_zero_cost_row(monkeypatch):
    fw._MODEL_CACHE.clear()
    _FakeModel.calls.clear()
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_pkg(_FakeModel))
    cfg = _cfg()
    cfg.data["transcription"]["local_model"] = "tiny"
    cfg.data["transcription"]["local_device"] = "cpu"
    cfg.data["transcription"]["local_compute_type"] = "int8"

    con = dbm.connect()
    dbm.add_channel(con, "UCfw0001", "https://youtube.com/@fw", "FW",
                    not_before=None)
    dbm.upsert_discovered(con, "vidfw001", "UCfw0001", "t", None)
    con.commit()

    audio = Path(_TMP) / "a3.m4a"
    audio.write_bytes(b"x" * 100)
    work = Path(_TMP) / "work3"
    work.mkdir(exist_ok=True)

    dest = fw.transcribe(cfg, con, "vidfw001", audio, 120.0, work)

    assert dest.name == "transcript.original.srt"
    body = dest.read_text(encoding="utf-8")
    assert "hello" in body and "world" in body
    assert "00:00:00,000 --> 00:00:02,000" in body

    row = con.execute(
        "SELECT provider, model, estimated_cost_usd FROM costs "
        "WHERE video_id=?", ("vidfw001",)).fetchone()
    assert row["provider"] == "faster_whisper"
    assert row["model"] == "tiny"
    assert row["estimated_cost_usd"] == 0.0

    # 配置里的模型档位/设备/量化真的传给了 WhisperModel(...)
    assert _FakeModel.calls[-1] == ("tiny", "cpu", "int8")

    # 这个测试文件里的其它测试（尤其最后那个跑 process_discovered 的）
    # 会扫全部 DISCOVERED 状态的视频——这里插入的 vidfw001 如果留着不清，
    # 会被后面的测试当成"新发现的视频"一并处理掉，assert_called_once
    # 就会因为多了一次意外调用而炸掉（同一个坑 test_p3_pipeline.py 里
    # test_process_discovered_fifo_prevents_starvation 也踩过一次）。
    con.execute("DELETE FROM costs WHERE video_id=?", ("vidfw001",))
    con.execute("DELETE FROM videos WHERE video_id=?", ("vidfw001",))
    con.commit()
    con.close()


def test_model_cached_across_calls(monkeypatch):
    fw._MODEL_CACHE.clear()
    _FakeModel.calls.clear()
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_pkg(_FakeModel))
    cfg = _cfg()
    audio = Path(_TMP) / "a4.m4a"
    audio.write_bytes(b"x" * 100)
    work = Path(_TMP) / "work4"
    work.mkdir(exist_ok=True)
    fw.transcribe(cfg, None, "vidfw002", audio, 60.0, work)
    fw.transcribe(cfg, None, "vidfw003", audio, 60.0, work)
    assert len(_FakeModel.calls) == 1, "同样的 (model,device,compute_type) 不该重复实例化"


def test_pipeline_dispatches_to_faster_whisper_when_primary_set():
    """跟 test_p3_pipeline.py 里 watchfolder 那条路径的写法对齐——只是
    primary 换成 faster_whisper，验证 process_audio_queue 真的调用了
    faster_whisper_backend.transcribe 而不是投进 watchfolder。"""
    cfg_mod.write_default_if_missing()
    cfg = cfg_mod.load()
    cfg.data["transcription"]["primary"] = "faster_whisper"
    con = dbm.connect()
    dbm.add_channel(con, "UCfw0002", "https://youtube.com/@fw2", "FW2",
                    not_before=None)
    log = RunLogger()
    stats = pipeline.RunStats()

    with mock.patch.object(pipeline.probe_mod, "probe",
                           return_value=ProbeResult(
                               "transcribe", title="t", duration_sec=90,
                               published_at="2026-07-18T00:00:00Z")):
        dbm.upsert_discovered(con, "vidfwdisp", "UCfw0002", "t", None)
        con.commit()
        pipeline.process_discovered(con, cfg, log, stats)
    assert dbm.get_video(con, "vidfwdisp")["status"] == st.AUDIO_QUEUED

    fake_audio = Path(_TMP) / "vidfwdisp.m4a"
    fake_audio.write_bytes(b"x" * 1024)
    fake_srt = Path(_TMP) / "vidfwdisp.srt"
    fake_srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n\n",
                        encoding="utf-8")

    with mock.patch.object(pipeline.dl, "download_audio",
                           return_value=DownloadResult(True, path=fake_audio)), \
         mock.patch("youtube_recorder.faster_whisper_backend.transcribe",
                    return_value=fake_srt) as m:
        pipeline.process_audio_queue(con, cfg, log, stats)

    m.assert_called_once()
    v = dbm.get_video(con, "vidfwdisp")
    assert v["status"] == st.TRANSCRIPT_READY
    art = dbm.get_artifact(con, "vidfwdisp", "srt_original")
    assert art and art["path"] == str(fake_srt)
    con.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all faster-whisper tests passed")
