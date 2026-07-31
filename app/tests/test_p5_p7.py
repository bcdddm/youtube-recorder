"""P5/P7 tests: chunking on real transcript, article flow with mocked LLM,
vault write mode A (immutable raw, updatable wiki, readback, dedup)."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytrec-p5-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import article as art_mod     # noqa: E402
from youtube_recorder import transcript as tr       # noqa: E402
from youtube_recorder import vault as vt            # noqa: E402
from youtube_recorder import providers              # noqa: E402

SPIKE_SRT = Path(__file__).resolve().parents[2] / "spike-results" / "nnze4i2Mt6o.srt"


def _canonical():
    return tr.canonicalize("nnze4i2Mt6o", SPIKE_SRT, duration_sec=1078,
                           source="macwhisper_srt")


def test_chunking_covers_everything():
    can = _canonical()
    chunks = art_mod.chunk_transcript(can)
    assert len(chunks) >= 2                       # 18-min video, ~8 min chunks
    assert chunks[0].first_segment == can.segments[0].segment_id
    assert chunks[-1].last_segment == can.segments[-1].segment_id
    total = sum(len(c.text) for c in chunks)
    assert total >= len(can.full_text) * 0.95     # nothing dropped in the middle
    for a, b in zip(chunks, chunks[1:]):
        assert a.end_ms <= b.start_ms + 1         # ordered, non-overlapping


FAKE_NOTE = json.dumps({"summary": "块摘要", "key_points": ["ASML财报超预期"],
                        "entities": ["ASML"], "numbers": ["8.3%"]})
FAKE_ARTICLE = json.dumps({
    "title_zh": "阿斯麦财报与半导体走势解析",
    "aliases": ["ASML财报解析"],
    "one_sentence": "美投侃新闻分析ASML财报后半导体板块的矛盾走势。",
    "summary": "本期分析ASML财报……",
    "sections": [{"heading": "财报要点", "body": "ASML交出亮眼财报……",
                  "source_chunk_ids": [0]},
                 {"heading": "市场反应", "body": "半导体不涨反跌……",
                  "source_chunk_ids": [1]}],
    "takeaways": ["财报好不等于股价涨"],
    "tags": ["半导体", "ASML"]})


class _FakeCfg:
    def __init__(self, root):  self.root = root
    def get(self, k, d=None):
        return {"article.mode": "edited_article",
                "article.verbatim_pct": 0,  # 该测试验证自由整理路径
                "article.provider": "anthropic",
                "vault.raw_subdir": "20-Raw/YouTube",
                "vault.wiki_subdir": "30-Wiki"}.get(k, d)
    @property
    def vault_root(self): return self.root


def test_article_generate_with_mock_llm():
    can = _canonical()
    replies = []
    def fake_complete(cfg, con, vid, system, user, max_tokens=0, purpose=""):
        replies.append(purpose)
        return FAKE_ARTICLE if "编辑" in system else FAKE_NOTE
    with mock.patch.object(providers, "complete", side_effect=fake_complete), \
         mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art = art_mod.generate(_FakeCfg(None), None, "nnze4i2Mt6o", can,
                               "原始标题", "美投侃新闻")
    assert art["title_zh"].startswith("阿斯麦")
    assert len(art["_chunks"]) == len(art_mod.chunk_transcript(can))
    n_chunks = len(art["_chunks"])
    assert replies.count("chunk_notes") == n_chunks   # every chunk analyzed


def test_existing_tag_vocab():
    """成文时优先复用已有标签词表：按频次降序（同频次按字典序），
    经 tags-merge.json 的归并映射合并同义词后再计数。"""
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir, ensure_dirs, APP_SUPPORT
    ensure_dirs()
    con = dbm.connect()
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
                "VALUES('UCvocab01','','v',1,?)", (dbm.now(),))
    tags_by_vid = {"voc1": ["AI", "美股"], "voc2": ["AI投资", "半导体"],
                   "voc3": ["半导体"]}
    for vid, tags in tags_by_vid.items():
        dbm.upsert_discovered(con, vid, "UCvocab01", vid, dbm.now())
        con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) "
                    "VALUES(?,'wiki','/tmp/x.md','h',?)", (vid, dbm.now()))
        wd = work_dir(vid); wd.mkdir(parents=True, exist_ok=True)
        (wd / "article.json").write_text(json.dumps({"tags": tags}, ensure_ascii=False))
    con.commit()
    (APP_SUPPORT / "tags-merge.json").write_text(
        json.dumps({"map": {"AI投资": "AI"}}, ensure_ascii=False), encoding="utf-8")

    vocab = art_mod.existing_tag_vocab(con)
    assert vocab[:2] == ["AI", "半导体"]  # 都经归并后出现2次，字典序在"美股"(1次)前面
    assert "美股" in vocab
    assert "AI投资" not in vocab  # 已被映射到 AI，不应该单独出现
    con.close()

    assert art_mod.existing_tag_vocab(None) == []  # con=None 安全返回空表


def test_compose_article_injects_existing_tags():
    """有已有标签库时，注入到 user message 里，提示 AI 优先复用。"""
    can = _canonical()
    captured = {}

    def fake_complete(cfg, con, vid, system, user, max_tokens=0, purpose=""):
        captured["user"] = user
        return FAKE_ARTICLE

    with mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art_mod.compose_article(_FakeCfg(None), None, "nnze4i2Mt6o", "标题", "频道",
                                [{"summary": "s"}], existing_tags=["AI", "半导体"])
    assert "已有标签库" in captured["user"]
    assert "AI" in captured["user"] and "半导体" in captured["user"]

    # 没有已有标签时不注入这段
    captured.clear()
    with mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art_mod.compose_article(_FakeCfg(None), None, "nnze4i2Mt6o", "标题", "频道",
                                [{"summary": "s"}])
    assert "已有标签库" not in captured["user"]


def test_existing_company_vocab():
    """公司/实体词表和标签词表分开维护，不经 tags-merge.json 归并，
    按出现频次降序（同频次按字典序）；con=None 安全返回空表。"""
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir, ensure_dirs
    ensure_dirs()
    con = dbm.connect()
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
                "VALUES('UCvocab02','','v2',1,?)", (dbm.now(),))
    seed = {"cvoc1": ["英伟达", "台积电"], "cvoc2": ["英伟达"], "cvoc3": ["特斯拉"]}
    for vid, companies in seed.items():
        dbm.upsert_discovered(con, vid, "UCvocab02", vid, dbm.now())
        con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) "
                    "VALUES(?,'wiki','/tmp/x.md','h',?)", (vid, dbm.now()))
        wd = work_dir(vid); wd.mkdir(parents=True, exist_ok=True)
        (wd / "article.json").write_text(
            json.dumps({"tags": [], "companies": companies}, ensure_ascii=False))
    con.commit()

    vocab = art_mod.existing_company_vocab(con)
    assert vocab[0] == "英伟达"  # 出现 2 次，排第一
    assert "台积电" in vocab and "特斯拉" in vocab
    con.close()

    assert art_mod.existing_company_vocab(None) == []  # con=None 安全返回空表


def test_compose_article_injects_existing_companies():
    """有已有公司库时，注入到 user message 里，提示 AI 优先复用同一写法。"""
    captured = {}

    def fake_complete(cfg, con, vid, system, user, max_tokens=0, purpose=""):
        captured["user"] = user
        return FAKE_ARTICLE

    with mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art_mod.compose_article(_FakeCfg(None), None, "nnze4i2Mt6o", "标题", "频道",
                                [{"summary": "s"}], existing_companies=["英伟达", "台积电"])
    assert "已有公司库" in captured["user"]
    assert "英伟达" in captured["user"] and "台积电" in captured["user"]

    # 没有已有公司库时不注入这段
    captured.clear()
    with mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art_mod.compose_article(_FakeCfg(None), None, "nnze4i2Mt6o", "标题", "频道",
                                [{"summary": "s"}])
    assert "已有公司库" not in captured["user"]


def test_generate_populates_companies_field():
    """generate() 会把已有标签库、已有公司库都注入 prompt；FAKE_ARTICLE 没有
    companies 字段时，art['companies'] 应安全兜底为空列表而不是缺失/报错。"""
    can = _canonical()

    def fake_complete(cfg, con, vid, system, user, max_tokens=0, purpose=""):
        return FAKE_ARTICLE if "编辑" in system else FAKE_NOTE

    with mock.patch.object(providers, "complete", side_effect=fake_complete), \
         mock.patch.object(art_mod.providers, "complete", side_effect=fake_complete):
        art = art_mod.generate(_FakeCfg(None), None, "nnze4i2Mt6o", can,
                               "原始标题", "美投侃新闻")
    assert art.get("companies") == []


def test_vault_mode_a():
    can = _canonical()
    root = Path(_TMP) / "vault"
    (root / "20-Raw/YouTube").mkdir(parents=True)
    (root / "30-Wiki").mkdir(parents=True)
    art = json.loads(FAKE_ARTICLE)
    art["_chunks"] = [{"chunk_id": 0, "start_ms": 0, "end_ms": 300000},
                      {"chunk_id": 1, "start_ms": 300000, "end_ms": 600000}]

    r1 = vt.write_raw_note(root, "20-Raw/YouTube", video_id="nnze4i2Mt6o",
                           video_title="阿斯麦引发恐慌？", channel="美投侃新闻",
                           published="2026-07-16T00:00:00Z",
                           video_url="https://youtu.be/nnze4i2Mt6o", can=can)
    assert r1 and r1.readback_ok and r1.created
    # immutable: second write is a no-op
    assert vt.write_raw_note(root, "20-Raw/YouTube", video_id="nnze4i2Mt6o",
                             video_title="改了标题也不覆盖", channel="x",
                             published="", video_url="u", can=can) is None

    content = vt.render_wiki_note(art, video_id="nnze4i2Mt6o",
                                  video_title="阿斯麦引发恐慌？",
                                  channel="美投侃新闻",
                                  published="2026-07-16T00:00:00Z",
                                  video_url="https://www.youtube.com/watch?v=nnze4i2Mt6o",
                                  raw_note_name=r1.path.stem)
    w1 = vt.write_wiki_note(root, "30-Wiki", content, "nnze4i2Mt6o",
                            art["title_zh"])
    assert w1.readback_ok and w1.created
    assert "youtube_video_id: nnze4i2Mt6o" in w1.path.read_text(encoding="utf-8")
    _txt = w1.path.read_text(encoding="utf-8")
    assert ("&t=" in _txt) or ("?t=" in _txt)             # time-coded source links

    # re-run with a different AI title -> updates SAME file, no duplicate
    art2 = dict(art, title_zh="完全不同的新标题")
    content2 = vt.render_wiki_note(art2, video_id="nnze4i2Mt6o",
                                   video_title="阿斯麦引发恐慌？",
                                   channel="美投侃新闻", published="",
                                   video_url="https://www.youtube.com/watch?v=nnze4i2Mt6o",
                                   raw_note_name=r1.path.stem)
    w2 = vt.write_wiki_note(root, "30-Wiki", content2, "nnze4i2Mt6o",
                            art2["title_zh"])
    assert not w2.created and w2.path == w1.path
    assert len(list((root / "30-Wiki").glob("*.md"))) == 1

    # path escape blocked
    try:
        vt.write_wiki_note(root, "../outside", "x", "vid", "t")
        raise AssertionError("escape allowed")
    except vt.VaultError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P5/P7 tests passed")


def test_local_provider_backends():
    from youtube_recorder import providers as pv
    from youtube_recorder.config import Config, DEFAULT_CONFIG, validate
    import copy, json
    # routing: local channel first, API fallbacks after
    cfg = Config(copy.deepcopy(DEFAULT_CONFIG))
    cfg.data["ai"]["article"] = "claude_cli"
    assert pv._route(cfg, "compose") == ["claude_cli", "openai", "anthropic"]
    cfg.data["ai"]["qa"] = "ollama"
    assert pv._route(cfg, "report_qa") == ["ollama", "openai", "anthropic"]
    # config validation accepts new routes
    d = copy.deepcopy(DEFAULT_CONFIG); d["ai"]["visuals"] = "ollama"
    assert validate(d) == []
    d["ai"]["visuals"] = "bogus"
    assert any("ai.visuals" in e for e in validate(d))
    # claude -p JSON parsing
    out = json.dumps({"type": "result", "is_error": False, "result": "你好",
                      "usage": {"input_tokens": 10, "output_tokens": 5}})
    assert pv._parse_claude_cli_json(out) == ("你好", 10, 5)
    bad = json.dumps({"is_error": True, "result": "limit reached"})
    try:
        pv._parse_claude_cli_json(bad); assert False
    except pv.ProviderError:
        pass
    # free providers never bill
    assert pv.FREE_PROVIDERS == {"claude_cli", "ollama"}


def test_complete_long_continuation():
    from youtube_recorder import providers as pv
    assert pv._looks_truncated("市场会首先") is True
    assert pv._looks_truncated("收尾。") is False
    assert pv._looks_truncated("done.") is False
    orig = pv.complete
    seq = ["前半被截断", "，后半补齐完成。", "。"]
    n = {"i": 0}
    def fake(cfg, con, vid, system, user, max_tokens=8000, purpose="article"):
        i = n["i"]; n["i"] += 1
        return seq[i] if i < len(seq) else "。"
    pv.complete = fake
    try:
        out = pv.complete_long(None, None, "v", "s", "u", max_rounds=3)
        assert out == "前半被截断，后半补齐完成。" and n["i"] == 2
        # non-truncated first shot → single round
        n["i"] = 0; seq[0] = "一次写完。"
        assert pv.complete_long(None, None, "v", "s", "u") == "一次写完。" and n["i"] == 1
    finally:
        pv.complete = orig
