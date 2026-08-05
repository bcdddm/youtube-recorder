"""Company Dossier plugin tests (default-off, config.dossier.enabled):
DB dedup tracking, AI extraction (mocked LLM), vault note read-modify-append
(new file / append-not-overwrite / exact-duplicate-line guard), pipeline
trigger gating, end-to-end orchestration, and the /companies GUI routes."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytrec-dossier-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod          # noqa: E402
from youtube_recorder import db as dbm                  # noqa: E402
from youtube_recorder import dossier                    # noqa: E402
from youtube_recorder import pipeline                    # noqa: E402
from youtube_recorder import providers                   # noqa: E402
from youtube_recorder.logging_setup import RunLogger      # noqa: E402
from youtube_recorder.paths import ensure_dirs, work_dir   # noqa: E402

ensure_dirs()
cfg_mod.write_default_if_missing()

FAKE_JSON = json.dumps({
    "observations": ["管理层对下半年订单展望乐观"],
    "concerns": ["地缘政治出口管制风险"],
    "price_levels": [{"text": "800 美元一线视为支撑", "price": 800,
                      "level_type": "support"}]})


class _CfgFlag:
    """Minimal cfg stub exposing only .get(), for pipeline-trigger tests."""
    def __init__(self, enabled=False):
        self._enabled = enabled

    def get(self, k, d=None):
        return self._enabled if k == "dossier.enabled" else d


# --- db.py: dedup tracking --------------------------------------------------

def test_dossier_dedup_tracking():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier1", "https://youtube.com/@d1", "D1")
    dbm.upsert_discovered(con, "vidDb01", "UCdossier1", "T", "2026-07-01T00:00:00Z")

    assert dbm.dossier_unprocessed(con, "vidDb01", []) == []
    todo = dbm.dossier_unprocessed(con, "vidDb01", ["ASML", "台积电"])
    assert todo == ["ASML", "台积电"]

    dbm.dossier_mark_processed(con, "vidDb01", "ASML")
    assert dbm.dossier_unprocessed(con, "vidDb01", ["ASML", "台积电"]) == ["台积电"]

    dbm.dossier_mark_processed(con, "vidDb01", "台积电")
    assert dbm.dossier_unprocessed(con, "vidDb01", ["ASML", "台积电"]) == []

    # idempotent: marking the same (company, video) twice doesn't error
    dbm.dossier_mark_processed(con, "vidDb01", "ASML")


# --- db.py: dossier_entities registry (alias resolution + pending queue) ---

def test_dossier_resolve_entity_registers_new_as_pending():
    con = dbm.connect()
    info = dbm.dossier_resolve_entity(con, "全新公司ABC")
    assert info == {"canonical": "全新公司ABC", "status": "pending",
                    "category": "entity", "is_new": True}
    row = dbm.dossier_get_entity(con, "全新公司ABC")
    assert row["status"] == "pending" and row["canonical"] is None

    # seeing it again is not "new" anymore, and it shows up in the pending queue
    info2 = dbm.dossier_resolve_entity(con, "全新公司ABC")
    assert info2["is_new"] is False
    pending_names = [r["name"] for r in dbm.dossier_pending_entities(con)]
    assert "全新公司ABC" in pending_names


def test_dossier_alias_resolves_to_canonical_and_its_status():
    con = dbm.connect()
    dbm.dossier_register_entity(con, "微软", category="entity", status="approved")
    dbm.dossier_set_entity_alias(con, "微软财报", "微软")
    info = dbm.dossier_resolve_entity(con, "微软财报")
    assert info["canonical"] == "微软"
    assert info["status"] == "approved"          # inherited from the target
    assert info["is_new"] is False
    # the alias itself never shows up as a pending item
    pending_names = [r["name"] for r in dbm.dossier_pending_entities(con)]
    assert "微软财报" not in pending_names


def test_dossier_set_entity_status_rejected():
    con = dbm.connect()
    dbm.dossier_register_entity(con, "18A测试", status="pending")
    dbm.dossier_set_entity_status(con, "18A测试", "rejected")
    info = dbm.dossier_resolve_entity(con, "18A测试")
    assert info["status"] == "rejected"


# --- dossier.py: entity resolution gates process_video_companies -----------

def test_process_video_companies_skips_rejected_entities():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier5", "https://youtube.com/@d5", "D5")
    dbm.upsert_discovered(con, "vidRej1", "UCdossier5", "T", "2026-07-10T00:00:00Z")
    dbm.dossier_register_entity(con, "18A", status="rejected")
    _write_article_json("vidRej1", ["18A"])
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidRej1", "wiki", "/fake/vault/30-Wiki/T--vidRej1.md", dbm.now()))
    con.commit()

    root = Path(_TMP) / "vault-rejected"

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            return root

    with mock.patch.object(providers, "complete", return_value=FAKE_JSON) as m:
        n = dossier.process_video_companies(_Cfg(), con, "vidRej1")
    assert n == 0
    m.assert_not_called()
    assert not dossier.dossier_note_path(root, "18A").exists()


def test_process_video_companies_redirects_alias_to_canonical():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier6", "https://youtube.com/@d6", "D6")
    dbm.upsert_discovered(con, "vidAlias1", "UCdossier6", "T", "2026-07-10T00:00:00Z")
    dbm.dossier_register_entity(con, "微软测试", status="approved")
    dbm.dossier_set_entity_alias(con, "微软测试财报", "微软测试")
    _write_article_json("vidAlias1", ["微软测试财报"])
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidAlias1", "wiki", "/fake/vault/30-Wiki/T--vidAlias1.md", dbm.now()))
    con.commit()

    root = Path(_TMP) / "vault-alias"

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            return root

    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        n = dossier.process_video_companies(_Cfg(), con, "vidAlias1")
    assert n == 1
    assert dossier.dossier_note_path(root, "微软测试").exists()
    assert not dossier.dossier_note_path(root, "微软测试财报").exists()


# --- dossier.py: archive / merge primitives (never hard-delete) ------------

def test_archive_entity_note_moves_not_deletes():
    root = Path(_TMP) / "vault-archive1"
    dossier.append_dossier_entries(
        root, "待归档公司", video_id="v1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n]]",
        points={"observations": ["内容"], "concerns": [], "price_levels": []})
    assert dossier.archive_entity_note(root, "待归档公司") is True
    assert not dossier.dossier_note_path(root, "待归档公司").exists()
    archived = dossier.archive_dir(root) / "待归档公司.md"
    assert archived.exists()
    assert "内容" in archived.read_text(encoding="utf-8")   # content preserved
    # missing note -> no-op, doesn't raise
    assert dossier.archive_entity_note(root, "不存在的公司") is False


def test_merge_entity_note_combines_content_and_archives_source():
    root = Path(_TMP) / "vault-merge1"
    dossier.append_dossier_entries(
        root, "PLTR测试", video_id="v1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n1]]",
        points={"observations": ["来自PLTR别名的观点"], "concerns": [],
               "price_levels": []})
    dossier.append_dossier_entries(
        root, "Palantir测试", video_id="v2", published="2026-07-02T00:00:00Z",
        channel="Y", source_link="[[n2]]",
        points={"observations": ["来自Palantir正名的观点"], "concerns": [],
               "price_levels": []})
    ok = dossier.merge_entity_note(root, "PLTR测试", "Palantir测试")
    assert ok is True
    # source archived, not deleted
    assert not dossier.dossier_note_path(root, "PLTR测试").exists()
    assert (dossier.archive_dir(root) / "PLTR测试.md").exists()
    # canonical note now has both notes' content
    merged = dossier.dossier_note_path(root, "Palantir测试").read_text(encoding="utf-8")
    assert "来自PLTR别名的观点" in merged
    assert "来自Palantir正名的观点" in merged


# --- dossier.py: ticker resolution + price history (chart data source) -----

def test_resolve_ticker_uses_static_map_without_ai_call():
    con = dbm.connect()
    with mock.patch.object(providers, "complete") as m:
        t = dossier.resolve_ticker(_CfgFlag(), con, "英伟达")
    assert t == "NVDA"
    m.assert_not_called()          # static map hit, no need to ask AI
    # cached: second lookup also skips both the map reasoning and any AI call
    with mock.patch.object(providers, "complete") as m2:
        t2 = dossier.resolve_ticker(_CfgFlag(), con, "英伟达")
    assert t2 == "NVDA"
    m2.assert_not_called()


def test_resolve_ticker_known_non_public_short_circuits():
    con = dbm.connect()
    with mock.patch.object(providers, "complete") as m:
        t = dossier.resolve_ticker(_CfgFlag(), con, "SpaceX")
    assert t is None
    m.assert_not_called()          # explicitly mapped to None, not "unknown"


def test_resolve_ticker_falls_back_to_ai_and_caches_result():
    con = dbm.connect()
    with mock.patch.object(providers, "complete", return_value="FAKE123") as m:
        t = dossier.resolve_ticker(_CfgFlag(), con, "一个不在映射表里的测试公司")
    assert t == "FAKE123"
    m.assert_called_once()
    # cached now -> second call doesn't hit the AI again
    with mock.patch.object(providers, "complete") as m2:
        t2 = dossier.resolve_ticker(_CfgFlag(), con, "一个不在映射表里的测试公司")
    assert t2 == "FAKE123"
    m2.assert_not_called()


def test_resolve_ticker_ai_says_none_caches_as_no_ticker():
    con = dbm.connect()
    with mock.patch.object(providers, "complete", return_value="NONE"):
        t = dossier.resolve_ticker(_CfgFlag(), con, "一个人物测试实体")
    assert t is None
    row = dbm.dossier_get_entity(con, "一个人物测试实体")
    assert row["ticker"] == ""     # cached as "checked, no ticker" not NULL


def test_fetch_price_history_handles_failure_gracefully():
    with mock.patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
        assert dossier.fetch_price_history("BOGUS-TICKER-XYZ") == []


# --- dossier.py: AI extraction (mocked LLM) ---------------------------------

def test_extract_company_points_success():
    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        out = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out == {
        "observations": ["管理层对下半年订单展望乐观"],
        "concerns": ["地缘政治出口管制风险"],
        "price_levels": [{"text": "800 美元一线视为支撑", "price": 800.0,
                          "level_type": "support"}],
    }


def test_extract_company_points_swallows_errors():
    with mock.patch.object(providers, "complete", side_effect=RuntimeError("boom")):
        out = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out == {"observations": [], "concerns": [], "price_levels": []}

    with mock.patch.object(providers, "complete", return_value="not json at all"):
        out2 = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out2 == {"observations": [], "concerns": [], "price_levels": []}


def test_extract_company_points_ignores_non_string_items():
    reply = json.dumps({"observations": ["ok", 123, "  ", None],
                        "concerns": "not-a-list",
                        "price_levels": [{"text": "1500 支撑", "price": 1500,
                                         "level_type": "support"},
                                        {"text": ""},   # empty text -> dropped
                                        "旧格式纯文本点位",  # legacy plain string
                                        {"text": "怪类型", "price": "n/a",
                                         "level_type": "bogus"},
                                        123]})
    with mock.patch.object(providers, "complete", return_value=reply):
        out = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out["observations"] == ["ok"]
    assert out["concerns"] == []          # non-list value -> empty, not an error
    assert out["price_levels"] == [
        {"text": "1500 支撑", "price": 1500.0, "level_type": "support"},
        {"text": "旧格式纯文本点位", "price": None, "level_type": "other"},
        {"text": "怪类型", "price": None, "level_type": "other"},
    ]


# --- dossier.py: vault note read-modify-append ------------------------------

def test_append_dossier_entries_new_file_then_append():
    root = Path(_TMP) / "vault-append"
    points1 = {"observations": ["看好长期成长"], "concerns": [],
              "price_levels": [{"text": "120 美元", "price": 120,
                                "level_type": "support"}]}
    ok = dossier.append_dossier_entries(
        root, "ASML测试", video_id="v1", published="2026-07-01T00:00:00Z",
        channel="美投侃新闻", source_link="[[note-v1]]", points=points1)
    assert ok is True

    path = dossier.dossier_note_path(root, "ASML测试")
    assert path.exists()
    txt = path.read_text(encoding="utf-8")
    assert "看好长期成长（来源：美投侃新闻 · [[note-v1]]）" in txt
    assert "120 美元（来源：美投侃新闻 · [[note-v1]]）" in txt
    assert txt.count("## 观点评价") == 1
    assert txt.count("## 关注点") == 1
    assert txt.count("## 推荐点位") == 1
    # observation landed under its own heading, not the price-levels one
    obs_idx = txt.find("## 观点评价")
    price_idx = txt.find("## 推荐点位")
    assert obs_idx < txt.find("看好长期成长") < price_idx

    # second call, different video, no channel known this time: appends,
    # doesn't overwrite prior content, citation gracefully drops the prefix
    points2 = {"observations": ["管理层下修指引"], "concerns": ["库存高企"],
              "price_levels": []}
    ok2 = dossier.append_dossier_entries(
        root, "ASML测试", video_id="v2", published="2026-07-05T00:00:00Z",
        channel=None, source_link="[[note-v2]]", points=points2)
    assert ok2 is True
    txt2 = path.read_text(encoding="utf-8")
    assert "看好长期成长" in txt2                      # old content preserved
    assert "管理层下修指引（来源：[[note-v2]]）" in txt2
    assert "库存高企（来源：[[note-v2]]）" in txt2
    m = re.search(r"(?m)^updated: (\d{4}-\d{2}-\d{2})$", txt2)
    assert m is not None                              # frontmatter updated

    # same points submitted again: exact-duplicate line is not inserted twice
    dossier.append_dossier_entries(
        root, "ASML测试", video_id="v2", published="2026-07-05T00:00:00Z",
        channel=None, source_link="[[note-v2]]", points=points2)
    txt3 = path.read_text(encoding="utf-8")
    assert txt3.count("管理层下修指引（来源：[[note-v2]]）") == 1


def test_append_dossier_entries_empty_points_is_noop():
    root = Path(_TMP) / "vault-noop"
    empty = {"observations": [], "concerns": [], "price_levels": []}
    ok = dossier.append_dossier_entries(
        root, "空壳公司", video_id="v1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[x]]", points=empty)
    assert ok is False
    assert not dossier.dossier_note_path(root, "空壳公司").exists()


def test_append_dossier_entries_writes_structured_price_levels_to_db():
    con = dbm.connect()
    root = Path(_TMP) / "vault-pricelevels"
    points = {"observations": [], "concerns": [],
             "price_levels": [{"text": "1200 视为压力位", "price": 1200,
                               "level_type": "resistance"},
                              {"text": "定性描述没有具体数字", "price": None,
                               "level_type": "other"}]}
    dossier.append_dossier_entries(
        root, "测试点位公司", video_id="vpl1", published="2026-07-01T00:00:00Z",
        channel="频道A", source_link="[[note-vpl1]]", points=points, con=con)
    rows = dbm.dossier_price_levels_for(con, "测试点位公司")
    assert len(rows) == 2
    by_type = {r["level_type"]: r for r in rows}
    assert by_type["resistance"]["price"] == 1200.0
    assert by_type["resistance"]["channel"] == "频道A"
    assert by_type["resistance"]["mentioned_date"] == "2026-07-01"
    assert by_type["other"]["price"] is None


# --- pipeline.py: trigger gating --------------------------------------------

def test_trigger_company_dossier_off_by_default():
    con = dbm.connect()
    log = RunLogger()
    stats = pipeline.RunStats()
    stats.vault_written_ids = ["vidTrig1"]
    with mock.patch.object(dossier, "process_video_companies") as m:
        pipeline._trigger_company_dossier(con, _CfgFlag(enabled=False), stats, log)
    m.assert_not_called()


def test_trigger_company_dossier_no_new_writes_skips():
    con = dbm.connect()
    log = RunLogger()
    stats = pipeline.RunStats()          # vault_written_ids stays empty
    with mock.patch.object(dossier, "process_video_companies") as m:
        pipeline._trigger_company_dossier(con, _CfgFlag(enabled=True), stats, log)
    m.assert_not_called()


def test_trigger_company_dossier_enabled_processes_each_written_id():
    con = dbm.connect()
    log = RunLogger()
    stats = pipeline.RunStats()
    stats.vault_written_ids = ["vidTrig2", "vidTrig3"]
    with mock.patch.object(dossier, "process_video_companies", return_value=1) as m:
        pipeline._trigger_company_dossier(con, _CfgFlag(enabled=True), stats, log)
    assert m.call_count == 2
    called_ids = [c.args[2] for c in m.call_args_list]
    assert called_ids == ["vidTrig2", "vidTrig3"]


def test_trigger_company_dossier_swallows_per_video_errors():
    con = dbm.connect()
    log = RunLogger()
    stats = pipeline.RunStats()
    stats.vault_written_ids = ["vidTrig4"]
    with mock.patch.object(dossier, "process_video_companies",
                           side_effect=RuntimeError("boom")):
        pipeline._trigger_company_dossier(con, _CfgFlag(enabled=True), stats, log)
    # no exception propagated — hook failure must never break the main run


# --- dossier.py: end-to-end orchestration -----------------------------------

def _write_article_json(video_id, companies):
    d = work_dir(video_id)
    art = {"title_zh": "标题", "summary": "摘要内容",
          "companies": companies,
          "sections": [{"heading": "要点", "body": "正文内容……"}]}
    (d / "article.json").write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")


def test_process_video_companies_end_to_end():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier2", "https://youtube.com/@d2", "D2")
    dbm.upsert_discovered(con, "vidE2E1", "UCdossier2", "标题", "2026-07-10T00:00:00Z")
    _write_article_json("vidE2E1", ["ASML", "台积电"])
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidE2E1", "wiki", "/fake/vault/30-Wiki/标题--vidE2E1.md", dbm.now()))
    con.commit()

    root = Path(_TMP) / "vault-e2e"

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            return root

    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        n = dossier.process_video_companies(_Cfg(), con, "vidE2E1")
    assert n == 2
    for name in ("ASML", "台积电"):
        p = dossier.dossier_note_path(root, name)
        assert p.exists()
        txt = p.read_text(encoding="utf-8")
        assert "[[标题--vidE2E1]]" in txt
        assert "管理层对下半年订单展望乐观" in txt

    # already-processed companies are skipped on a second call (dedup)
    with mock.patch.object(providers, "complete", return_value=FAKE_JSON) as m:
        n2 = dossier.process_video_companies(_Cfg(), con, "vidE2E1")
    assert n2 == 0
    m.assert_not_called()


def test_process_video_companies_no_article_json_is_safe():
    con = dbm.connect()
    assert dossier.process_video_companies(_CfgFlag(), con, "vid-does-not-exist") == 0


# --- dossier.py: historical backfill ----------------------------------------

def test_backfill_all_processes_every_wiki_write_once():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier3", "https://youtube.com/@d3", "D3")

    # two videos with wiki writes + article.json companies, one without
    # article.json at all (should just be skipped, not error the whole run)
    for i, comps in enumerate([["ASML"], ["台积电", "英伟达"]], start=1):
        vid = f"vidBF0{i}"
        dbm.upsert_discovered(con, vid, "UCdossier3", "标题", "2026-07-10T00:00:00Z")
        _write_article_json(vid, comps)
        con.execute(
            "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
            (vid, "wiki", f"/fake/vault/30-Wiki/标题--{vid}.md", dbm.now()))
    dbm.upsert_discovered(con, "vidBF03", "UCdossier3", "无文章", "2026-07-11T00:00:00Z")
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidBF03", "wiki", "/fake/vault/30-Wiki/无文章--vidBF03.md", dbm.now()))
    con.commit()

    root = Path(_TMP) / "vault-backfill"

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            return root

    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        result = dossier.backfill_all(_Cfg(), con)
    assert result["scanned"] >= 3          # at least our 3 fixture videos
    assert result["videos_with_companies"] >= 2
    assert result["companies_processed"] >= 3   # ASML + 台积电 + 英伟达

    for name in ("ASML", "台积电", "英伟达"):
        assert dossier.dossier_note_path(root, name).exists()

    # running it again is a no-op for those same videos (already processed)
    with mock.patch.object(providers, "complete", return_value=FAKE_JSON) as m:
        dossier.backfill_all(_Cfg(), con)
    m.assert_not_called()


def test_backfill_all_skips_broken_video_without_failing_the_batch():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier4", "https://youtube.com/@d4", "D4")
    dbm.upsert_discovered(con, "vidBFerr", "UCdossier4", "T", "2026-07-10T00:00:00Z")
    _write_article_json("vidBFerr", ["ASML"])
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidBFerr", "wiki", "/fake/vault/30-Wiki/T--vidBFerr.md", dbm.now()))
    con.commit()

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            raise RuntimeError("boom")   # simulate a broken lookup for this video

    result = dossier.backfill_all(_Cfg(), con)   # must not raise
    assert result["scanned"] >= 1


def test_rescan_all_archives_notes_resets_and_rebuilds():
    con = dbm.connect()
    dbm.add_channel(con, "UCdossier7", "https://youtube.com/@d7", "D7")
    dbm.upsert_discovered(con, "vidRS1", "UCdossier7", "标题", "2026-07-10T00:00:00Z")
    _write_article_json("vidRS1", ["重扫测试公司"])
    con.execute(
        "INSERT INTO writes(video_id, note_kind, note_path, at) VALUES (?,?,?,?)",
        ("vidRS1", "wiki", "/fake/vault/30-Wiki/标题--vidRS1.md", dbm.now()))
    con.commit()

    root = Path(_TMP) / "vault-rescan"

    class _Cfg:
        def get(self, k, d=None):
            return d

        @property
        def vault_root(self):
            return root

    # first pass: old-style content (pretend it predates the channel-prefix
    # and structured price-level upgrade)
    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        n1 = dossier.process_video_companies(_Cfg(), con, "vidRS1")
    assert n1 == 1
    old_path = dossier.dossier_note_path(root, "重扫测试公司")
    assert old_path.exists()
    assert len(dbm.dossier_price_levels_for(con, "重扫测试公司")) == 1

    # rescan: archives the old note, wipes processed/price-level state,
    # and rebuilds everything fresh via backfill_all()
    new_reply = json.dumps({
        "observations": ["重扫后的新观点"], "concerns": [],
        "price_levels": [{"text": "重扫后的新点位", "price": 900,
                          "level_type": "target"}]})
    with mock.patch.object(providers, "complete", return_value=new_reply):
        result = dossier.rescan_all(_Cfg(), con)
    assert result["archived_notes"] >= 1
    assert result["companies_processed"] >= 1

    # old note content preserved in the archive folder (not deleted)
    archived_files = list(dossier.archive_dir(root).glob("重扫测试公司*.md"))
    assert archived_files
    assert "800 美元一线视为支撑" in archived_files[0].read_text(encoding="utf-8")

    # main note now has the freshly-regenerated content instead
    new_txt = old_path.read_text(encoding="utf-8")
    assert "重扫后的新观点" in new_txt
    assert "800 美元一线视为支撑" not in new_txt   # not duplicated from the old run

    levels = dbm.dossier_price_levels_for(con, "重扫测试公司")
    assert len(levels) == 1
    assert levels[0]["price"] == 900.0


# --- gui.py: /companies list + detail routes --------------------------------

def test_companies_routes():
    from youtube_recorder import gui
    client = gui.app.test_client()

    # phase 1: no vault root configured yet -> that guard fires first, nav
    # has no "公司档案" entry since the plugin defaults to off
    r = client.get("/companies")
    assert r.status_code == 200
    assert "还没配置保存根目录".encode() in r.data
    assert b'href="/companies"' not in client.get("/settings").data

    # phase 1b: vault root set but plugin still off -> distinct message
    gvault = Path(_TMP) / "vault-gui"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg_mod.save(cfg)
    r = client.get("/companies")
    assert "还没开启".encode() in r.data
    assert b'href="/companies"' not in client.get("/settings").data

    # phase 2: enable the plugin against that same empty vault -> empty-state
    cfg = cfg_mod.load()
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    r = client.get("/companies")
    assert r.status_code == 200
    assert "还没有公司档案".encode() in r.data
    assert b'href="/companies"' in client.get("/settings").data

    # phase 3: write two dossier notes directly, then list + view them
    dossier.append_dossier_entries(
        gvault, "英伟达", video_id="vg1", published="2026-07-01T00:00:00Z",
        channel="频道甲", source_link="[[note-vg1]]",
        points={"observations": ["数据中心需求强劲"], "concerns": [],
               "price_levels": [{"text": "120 美元一线", "price": 120,
                                 "level_type": "support"}]})
    dossier.append_dossier_entries(
        gvault, "台积电", video_id="vg2", published="2026-07-02T00:00:00Z",
        channel="频道乙", source_link="[[note-vg2]]",
        points={"observations": [], "concerns": ["先进制程扩产不及预期"],
               "price_levels": []})

    r = client.get("/companies")
    assert r.status_code == 200
    assert "英伟达".encode() in r.data
    assert "台积电".encode() in r.data

    # avoid a real network hit to Yahoo Finance during tests
    with mock.patch.object(dossier, "fetch_price_history", return_value=[]):
        r = client.get("/companies/英伟达")
    assert r.status_code == 200
    assert "数据中心需求强劲".encode() in r.data
    assert "120 美元一线".encode() in r.data
    assert "频道甲".encode() in r.data             # channel name shown
    # scroll position is preserved across form-submit reloads (delete/pin/etc
    # shouldn't jump the page back to the top)
    assert b"ytrec_scroll_" in r.data

    assert client.get("/companies/不存在的公司").status_code == 404


def test_companies_pending_queue_approve_and_reject():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-pending"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dbm.dossier_register_entity(con, "待批准测试公司", status="pending")
    dossier.append_dossier_entries(
        gvault, "待批准测试公司", video_id="vp1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n]]",
        points={"observations": ["内容"], "concerns": [], "price_levels": []})

    r = client.get("/companies")
    assert "检测到".encode() in r.data
    assert "待批准测试公司".encode() in r.data

    # approve: moves into the main table (still shows on next load)
    r = client.post("/companies/approve", data={"_csrf": gui.CSRF,
                    "name": "待批准测试公司"}, follow_redirects=True)
    assert r.status_code == 200
    info = dbm.dossier_resolve_entity(con, "待批准测试公司")
    assert info["status"] == "approved"

    # a second pending one gets rejected -> archived, not deleted
    dbm.dossier_register_entity(con, "待拒绝测试实体", status="pending")
    dossier.append_dossier_entries(
        gvault, "待拒绝测试实体", video_id="vp2", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n]]",
        points={"observations": ["占位内容"], "concerns": [], "price_levels": []})
    r = client.post("/companies/reject", data={"_csrf": gui.CSRF,
                    "name": "待拒绝测试实体"}, follow_redirects=True)
    assert r.status_code == 200
    assert dbm.dossier_resolve_entity(con, "待拒绝测试实体")["status"] == "rejected"
    assert not dossier.dossier_note_path(gvault, "待拒绝测试实体").exists()
    assert (dossier.archive_dir(gvault) / "待拒绝测试实体.md").exists()
    assert "待拒绝测试实体".encode() not in client.get("/companies").data


def test_companies_pin_and_collapse():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-pin"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    for n in ("置顶测试甲", "置顶测试乙", "折叠测试丙"):
        dossier.append_dossier_entries(
            gvault, n, video_id=f"vp_{n}", published="2026-07-01T00:00:00Z",
            channel="X", source_link="[[n]]",
            points={"observations": ["内容"], "concerns": [], "price_levels": []})

    con = dbm.connect()

    # pin 甲 then 乙 -> 乙 (pinned later) sits above 甲 in the pinned block
    r = client.post("/companies/pin", data={"_csrf": gui.CSRF, "name": "置顶测试甲"},
                    follow_redirects=True)
    assert r.status_code == 200
    r = client.post("/companies/pin", data={"_csrf": gui.CSRF, "name": "置顶测试乙"},
                    follow_redirects=True)
    body = r.data.decode()
    assert "📌 置顶" in body
    pos_a = body.index("置顶测试甲")
    pos_b = body.index("置顶测试乙")
    assert pos_b < pos_a          # most-recently-pinned floats to the top

    # collapse 丙 -> disappears from the main table, shows under 已折叠
    r = client.post("/companies/collapse", data={"_csrf": gui.CSRF, "name": "折叠测试丙"},
                    follow_redirects=True)
    body = r.data.decode()
    assert "已折叠 1 个" in body
    main_table = body.split("已折叠", 1)[0]
    assert "折叠测试丙" not in main_table

    # uncollapse brings it back into the regular list
    r = client.post("/companies/uncollapse", data={"_csrf": gui.CSRF, "name": "折叠测试丙"},
                    follow_redirects=True)
    body = r.data.decode()
    assert "折叠测试丙" in body
    assert "已折叠" not in body

    # unpin 甲 -> leaves the pinned block, 乙 stays pinned
    r = client.post("/companies/unpin", data={"_csrf": gui.CSRF, "name": "置顶测试甲"},
                    follow_redirects=True)
    body = r.data.decode()
    pinned_block = body.split("📌 置顶", 1)[1].split("</table>", 1)[0]
    assert "置顶测试甲" not in pinned_block
    assert "置顶测试乙" in pinned_block

    ent = dbm.dossier_get_entity(con, "置顶测试甲")
    assert ent["pinned"] == 0


def test_company_view_redirects_alias_to_canonical():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-alias"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dbm.dossier_register_entity(con, "谷歌测试", status="approved")
    dbm.dossier_set_entity_alias(con, "阿法贝测试", "谷歌测试")
    dossier.append_dossier_entries(
        gvault, "谷歌测试", video_id="vg1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n]]",
        points={"observations": ["内容"], "concerns": [], "price_levels": []})

    r = client.get("/companies/阿法贝测试", follow_redirects=False)
    assert r.status_code == 302
    import urllib.parse
    assert urllib.parse.unquote(r.headers["Location"]).endswith("/companies/谷歌测试")


def test_company_view_renders_price_level_table_and_clickable_source():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-levels"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dossier.append_dossier_entries(
        gvault, "点位表测试公司", video_id="vt1", published="2026-07-03T00:00:00Z",
        channel="频道丙", source_link="[[标题--realvid123]]",
        points={"observations": [], "concerns": [],
               "price_levels": [{"text": "2000 是压力位", "price": 2000,
                                 "level_type": "resistance"}]},
        con=con)

    # avoid a real AI/network hit for ticker resolution during tests
    with mock.patch.object(dossier, "resolve_ticker", return_value=None):
        r = client.get("/companies/点位表测试公司")
    assert r.status_code == 200
    assert "推荐点位一览".encode() in r.data
    assert "压力位".encode() in r.data
    assert b"2000" in r.data
    assert "频道丙".encode() in r.data
    # the [[标题--realvid123]] citation became a real clickable link
    assert b'href="/reports/realvid123"' in r.data
    assert "没能识别出对应的股票代码".encode() in r.data
    # delete button has no confirm() prompt — deletes immediately
    assert b"priceLevelTable" in r.data
    assert b'data-date="2026-07-03"' in r.data
    del_form_idx = r.data.find(b"price-level/delete")
    form_snippet = r.data[del_form_idx - 40:del_form_idx + 200]
    assert b"confirm(" not in form_snippet


def test_dossier_chart_renders_line_and_scatter_when_ticker_and_history_found():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-chart"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dossier.append_dossier_entries(
        gvault, "图表测试公司", video_id="vc1", published="2026-07-01T00:00:00Z",
        channel="X", source_link="[[n]]",
        points={"observations": [], "concerns": [],
               "price_levels": [{"text": "100 支撑", "price": 100,
                                 "level_type": "support"}]},
        con=con)
    fake_hist = [{"date": "2026-07-01", "close": 95.0},
                {"date": "2026-07-02", "close": 101.0}]
    with mock.patch.object(dossier, "resolve_ticker", return_value="FAKE"), \
         mock.patch.object(dossier, "fetch_price_history", return_value=fake_hist):
        r = client.get("/companies/图表测试公司")
    assert r.status_code == 200
    assert b"chart.js" in r.data
    assert "价格走势与推荐点位（FAKE）".encode() in r.data
    assert b"2026-07-01" in r.data and b"95.0" in r.data
    assert b"pointStyle" in r.data and b"dash" in r.data   # short-dash markers
    assert b"channel" in r.data and b"X" in r.data          # per-channel grouping
    assert b"#d16060" in r.data                              # palette color assigned
    # time-range quick filters (近1月/近3月/近6月/近1年/全部) + JS to filter
    # both the chart and the price-level table in sync
    for label in ("近1月", "近3月", "近6月", "近1年", "全部"):
        assert label.encode() in r.data
    assert b"data-range-for=" in r.data
    assert b"applyRange" in r.data
    assert b"priceLevelTable" in r.data


def test_dossier_chart_groups_points_by_channel_with_distinct_colors():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-chart-multi"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dossier.append_dossier_entries(
        gvault, "多频道图表测试公司", video_id="vc2", published="2026-07-01T00:00:00Z",
        channel="频道甲", source_link="[[n1]]",
        points={"observations": [], "concerns": [],
               "price_levels": [{"text": "甲说的点位", "price": 100,
                                 "level_type": "support"}]},
        con=con)
    dossier.append_dossier_entries(
        gvault, "多频道图表测试公司", video_id="vc3", published="2026-07-02T00:00:00Z",
        channel="频道乙", source_link="[[n2]]",
        points={"observations": [], "concerns": [],
               "price_levels": [{"text": "乙说的点位", "price": 110,
                                 "level_type": "resistance"}]},
        con=con)
    fake_hist = [{"date": "2026-07-01", "close": 95.0},
                {"date": "2026-07-02", "close": 101.0}]
    with mock.patch.object(dossier, "resolve_ticker", return_value="FAKE2"), \
         mock.patch.object(dossier, "fetch_price_history", return_value=fake_hist):
        r = client.get("/companies/多频道图表测试公司")
    assert r.status_code == 200
    body = r.data.decode()
    assert "频道甲" in body and "频道乙" in body
    # two distinct channels -> two distinct palette colors used
    assert "#d16060" in body and "#7aa2f7" in body
    assert "甲说的点位" in body and "乙说的点位" in body


def test_filter_price_level_outliers_drops_20x_off_points():
    rows = [
        {"price": 120}, {"price": 150}, {"price": 160},   # cluster near ref
        {"price": 5},                                       # < ref/20 -> dropped
        {"price": 5000},                                    # > ref*20 -> dropped
    ]
    kept, excluded = dossier.filter_price_level_outliers(rows, reference=130)
    assert [r["price"] for r in kept] == [120, 150, 160]
    assert [r["price"] for r in excluded] == [5, 5000]


def test_filter_price_level_outliers_falls_back_to_median_without_reference():
    rows = [{"price": 100}, {"price": 110}, {"price": 105}, {"price": 3000}]
    kept, excluded = dossier.filter_price_level_outliers(rows, reference=None)
    assert 3000 not in [r["price"] for r in kept]
    assert excluded and excluded[0]["price"] == 3000


def test_company_view_hides_outlier_levels_and_shows_note():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-outlier"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dossier.append_dossier_entries(
        gvault, "跑偏点位测试公司", video_id="vo1", published="2026-07-01T00:00:00Z",
        channel="频道丁", source_link="[[n]]",
        points={"observations": [], "concerns": [],
               "price_levels": [
                   {"text": "正常点位", "price": 100, "level_type": "support"},
                   {"text": "这是别的东西的数字混进来了", "price": 5000, "level_type": "other"},
               ]},
        con=con)
    fake_hist = [{"date": "2026-07-01", "close": 101.0}]
    with mock.patch.object(dossier, "resolve_ticker", return_value="OUT"), \
         mock.patch.object(dossier, "fetch_price_history", return_value=fake_hist):
        r = client.get("/companies/跑偏点位测试公司")
    assert r.status_code == 200
    body = r.data.decode()
    assert "正常点位" in body
    # the outlier's raw text still appears in the underlying note prose (never
    # deleted), but must not appear in the structured price-level table
    table_html = body.split("推荐点位一览", 1)[1].split("</table>", 1)[0]
    assert "这是别的东西的数字混进来了" not in table_html
    assert "5000" not in table_html
    assert "已自动隐藏 1 条" in body


def test_company_price_level_delete_removes_row():
    from youtube_recorder import gui
    client = gui.app.test_client()

    gvault = Path(_TMP) / "vault-gui-delete"
    gvault.mkdir(parents=True, exist_ok=True)
    cfg = cfg_mod.load()
    cfg.data["vault"]["root"] = str(gvault)
    cfg.data.setdefault("dossier", {})["enabled"] = True
    cfg_mod.save(cfg)

    con = dbm.connect()
    dossier.append_dossier_entries(
        gvault, "删除点位测试公司", video_id="vd1", published="2026-07-01T00:00:00Z",
        channel="频道戊", source_link="[[n]]",
        points={"observations": [], "concerns": [],
               "price_levels": [{"text": "待删除的点位", "price": 42,
                                 "level_type": "support"}]},
        con=con)
    levels = dbm.dossier_price_levels_for(con, "删除点位测试公司")
    assert len(levels) == 1
    level_id = levels[0]["id"]

    with mock.patch.object(dossier, "resolve_ticker", return_value=None):
        r = client.post("/companies/删除点位测试公司/price-level/delete",
                        data={"_csrf": gui.CSRF, "level_id": str(level_id)},
                        follow_redirects=False)
    assert r.status_code in (302, 303)
    remaining = dbm.dossier_price_levels_for(con, "删除点位测试公司")
    assert remaining == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all dossier tests passed")
