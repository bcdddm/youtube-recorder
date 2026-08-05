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
    "price_levels": ["800 美元一线视为支撑"]})


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


# --- dossier.py: AI extraction (mocked LLM) ---------------------------------

def test_extract_company_points_success():
    with mock.patch.object(providers, "complete", return_value=FAKE_JSON):
        out = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out == {
        "observations": ["管理层对下半年订单展望乐观"],
        "concerns": ["地缘政治出口管制风险"],
        "price_levels": ["800 美元一线视为支撑"],
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
                        "concerns": "not-a-list", "price_levels": []})
    with mock.patch.object(providers, "complete", return_value=reply):
        out = dossier.extract_company_points(_CfgFlag(), None, "vidX", "ASML", "正文……")
    assert out["observations"] == ["ok"]
    assert out["concerns"] == []          # non-list value -> empty, not an error
    assert out["price_levels"] == []


# --- dossier.py: vault note read-modify-append ------------------------------

def test_append_dossier_entries_new_file_then_append():
    root = Path(_TMP) / "vault-append"
    points1 = {"observations": ["看好长期成长"], "concerns": [],
              "price_levels": ["120 美元"]}
    ok = dossier.append_dossier_entries(
        root, "ASML测试", video_id="v1", published="2026-07-01T00:00:00Z",
        source_link="[[note-v1]]", points=points1)
    assert ok is True

    path = dossier.dossier_note_path(root, "ASML测试")
    assert path.exists()
    txt = path.read_text(encoding="utf-8")
    assert "看好长期成长（来源：[[note-v1]]）" in txt
    assert "120 美元（来源：[[note-v1]]）" in txt
    assert txt.count("## 观点评价") == 1
    assert txt.count("## 关注点") == 1
    assert txt.count("## 推荐点位") == 1
    # observation landed under its own heading, not the price-levels one
    obs_idx = txt.find("## 观点评价")
    price_idx = txt.find("## 推荐点位")
    assert obs_idx < txt.find("看好长期成长") < price_idx

    # second call, different video: appends, doesn't overwrite prior content
    points2 = {"observations": ["管理层下修指引"], "concerns": ["库存高企"],
              "price_levels": []}
    ok2 = dossier.append_dossier_entries(
        root, "ASML测试", video_id="v2", published="2026-07-05T00:00:00Z",
        source_link="[[note-v2]]", points=points2)
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
        source_link="[[note-v2]]", points=points2)
    txt3 = path.read_text(encoding="utf-8")
    assert txt3.count("管理层下修指引（来源：[[note-v2]]）") == 1


def test_append_dossier_entries_empty_points_is_noop():
    root = Path(_TMP) / "vault-noop"
    empty = {"observations": [], "concerns": [], "price_levels": []}
    ok = dossier.append_dossier_entries(
        root, "空壳公司", video_id="v1", published="2026-07-01T00:00:00Z",
        source_link="[[x]]", points=empty)
    assert ok is False
    assert not dossier.dossier_note_path(root, "空壳公司").exists()


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
        source_link="[[note-vg1]]",
        points={"observations": ["数据中心需求强劲"], "concerns": [],
               "price_levels": ["120 美元一线"]})
    dossier.append_dossier_entries(
        gvault, "台积电", video_id="vg2", published="2026-07-02T00:00:00Z",
        source_link="[[note-vg2]]",
        points={"observations": [], "concerns": ["先进制程扩产不及预期"],
               "price_levels": []})

    r = client.get("/companies")
    assert r.status_code == 200
    assert "英伟达".encode() in r.data
    assert "台积电".encode() in r.data

    r = client.get("/companies/英伟达")
    assert r.status_code == 200
    assert "数据中心需求强劲".encode() in r.data
    assert "120 美元一线".encode() in r.data
    assert "note-vg1".encode() in r.data          # source citation rendered

    assert client.get("/companies/不存在的公司").status_code == 404


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all dossier tests passed")
