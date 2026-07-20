"""P6 tests: cue recall on the real spike transcript, density gating, dedup."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytrec-p6-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import article as art_mod   # noqa: E402
from youtube_recorder import transcript as tr     # noqa: E402
from youtube_recorder import visuals as vz        # noqa: E402

SPIKE_SRT = Path(__file__).resolve().parents[2] / "spike-results" / "nnze4i2Mt6o.srt"


def _setup():
    can = tr.canonicalize("nnze4i2Mt6o", SPIKE_SRT, duration_sec=1078,
                          source="macwhisper_srt")
    return can, art_mod.chunk_transcript(can)


def test_recall_on_real_transcript():
    can, chunks = _setup()
    # 财经视频里"我们来看/走势"等提示应该能召回到候选
    c3 = vz.recall(can, chunks, density=3)
    c1 = vz.recall(can, chunks, density=1)
    c5 = vz.recall(can, chunks, density=5)
    assert len(c1) <= len(c3) <= len(c5)          # density单调
    assert len(c1) <= 3 and len(c3) <= 8          # soft_max 生效
    for c in c3:
        assert c.window_ms[0] <= c.target_ms <= c.window_ms[1]
        assert c.chunk_id is not None             # 能映射回文章块
    # 最小间距
    thr, spacing, _ = vz.DENSITY[3]
    for a, b in zip(c3, c3[1:]):
        assert b.target_ms - a.target_ms >= spacing * 1000


def test_explicit_cues_matched():
    class Seg:
        def __init__(self, i, ms, t):
            self.segment_id = f"s{i:04d}"; self.start_ms = ms
            self.end_ms = ms + 3000; self.text = t
    class Can:
        segments = [Seg(0, 1000, "大家看这张表，第二列增长最明显"),
                    Seg(1, 200000, "as you can see on screen, the chart shows growth"),
                    Seg(2, 400000, "这纯粹是想象一下的比喻，没有画面")]
    class Chunk:
        def __init__(s): s.chunk_id, s.start_ms, s.end_ms = 0, 0, 500000
    cands = vz.recall(Can(), [Chunk()], density=3)
    ids = [c.segment_id for c in cands]
    assert "s0000" in ids and "s0001" in ids
    assert "s0002" not in ids                      # 比喻不触发


def test_dedup_and_hash():
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow absent — hash fallback path only)")
        return
    d = Path(_TMP)
    a, b, c = d / "a.jpg", d / "b.jpg", d / "c.jpg"
    Image.new("RGB", (320, 180), (200, 30, 30)).save(a)
    Image.new("RGB", (320, 180), (201, 31, 31)).save(b)   # 近重复
    im = Image.new("RGB", (320, 180), (10, 10, 10))
    for x in range(0, 320, 20):
        for y in range(0, 180, 2):
            im.putpixel((x, y), (255, 255, 255))
    im.save(c)                                            # 明显不同
    ha, hb, hc = vz.ahash(a), vz.ahash(b), vz.ahash(c)
    assert vz.hamming(ha, hb) <= 5                        # 判为重复
    assert vz.hamming(ha, hc) > 5                         # 判为不同


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P6 tests passed")
