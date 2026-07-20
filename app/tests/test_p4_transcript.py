"""P4 tests — uses the REAL spike SRT (contains a genuine trailing
hallucination + out-of-bounds timestamp)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytrec-p4-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import transcript as tr  # noqa: E402

SPIKE_SRT = Path(__file__).resolve().parents[2] / "spike-results" / "nnze4i2Mt6o.srt"


def test_real_spike_srt():
    assert SPIKE_SRT.exists(), f"missing fixture {SPIKE_SRT}"
    can = tr.canonicalize("nnze4i2Mt6o", SPIKE_SRT, duration_sec=1078,
                          source="macwhisper_srt")
    # the Russian hallucination tail must be gone
    assert "Продолжение" not in can.full_text
    # no segment may exceed duration + tolerance
    limit = 1078 * 1000 + tr.DURATION_TOLERANCE_MS
    assert all(s.end_ms <= limit for s in can.segments)
    assert all(can.segments[i].start_ms <= can.segments[i + 1].start_ms
               for i in range(len(can.segments) - 1))
    assert len(can.segments) >= 495           # ~501 minus trimmed
    assert can.coverage() > 0.6
    assert any("hallucination" in w or "beyond_duration" in w for w in can.warnings)
    # roundtrip
    can2 = tr.Canonical.from_json(can.to_json())
    assert len(can2.segments) == len(can.segments)


def test_vtt_and_loop_collapse():
    p = Path(_TMP) / "x.vtt"
    p.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n<v Speaker>hello <b>world</b>\n\n"
        + "".join(f"00:00:0{i}.000 --> 00:00:0{i}.500\nsame line\n\n"
                  for i in range(2, 9))
        + "00:00:09.000 --> 00:00:10.000\nbye\n\n",
        encoding="utf-8")
    can = tr.canonicalize("vttvid", p, duration_sec=10, source="youtube_captions")
    assert can.segments[0].text == "hello world"          # tags stripped
    same = [s for s in can.segments if s.text == "same line"]
    assert len(same) <= tr.MAX_REPEAT_RUN + 1             # loop collapsed
    assert can.segments[-1].text == "bye"


def test_garbage_rejected():
    p = Path(_TMP) / "empty.srt"
    p.write_text("not a subtitle file at all", encoding="utf-8")
    try:
        tr.canonicalize("bad", p, duration_sec=100, source="macwhisper_srt")
        raise AssertionError("garbage accepted")
    except tr.TranscriptInvalid:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P4 tests passed")
