"""P9 GUI tests: pages render, CSRF enforced, vault path escape blocked."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytrec-p9-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod   # noqa: E402
from youtube_recorder import db as dbm           # noqa: E402
from youtube_recorder.paths import ensure_dirs   # noqa: E402

ensure_dirs()
cfg_mod.write_default_if_missing()

# set a vault root inside tmp
_vault = Path(_TMP) / "vault"
(_vault / "30-Wiki").mkdir(parents=True)
cfg = cfg_mod.load()
cfg.data["vault"]["root"] = str(_vault)
cfg_mod.save(cfg)

from youtube_recorder import gui                 # noqa: E402

client = gui.app.test_client()


def test_pages_render():
    for path in ("/channels", "/queue", "/reports", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "YouTube Recorder".encode() in r.data
    assert "By Leoluchino".encode() in client.get("/settings").data


def test_csrf_enforced():
    r = client.post("/channels", data={"url": "https://www.youtube.com/@x"})
    assert r.status_code == 403
    r = client.post("/settings", data={"form": "keys"})
    assert r.status_code == 403


def test_vault_path_escape_blocked():
    secret = Path(_TMP) / "secret.jpg"
    secret.write_bytes(b"x")
    r = client.get("/vault-file?p=../secret.jpg")
    assert r.status_code == 403
    r = client.get("/vault-file?p=/etc/passwd")
    assert r.status_code == 403
    # legit image inside vault works
    ok = _vault / "40-Attachments" / "a.jpg"
    ok.parent.mkdir(parents=True, exist_ok=True)
    ok.write_bytes(b"\xff\xd8\xff\xe0fakejpg")
    r = client.get("/vault-file?p=40-Attachments/a.jpg")
    assert r.status_code == 200


def test_report_route_404_for_unknown():
    assert client.get("/reports/doesnotexist").status_code == 404


def test_schedule_render():
    from youtube_recorder import scheduler
    xml = scheduler.render_plist([8, 10, 22])
    assert xml.count("<key>Hour</key>") == 3
    assert "<integer>22</integer>" in xml
    assert "com.leoluchino.youtube-recorder" in xml
    try:
        scheduler.render_plist([])
    except Exception:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all P9 tests passed")


def test_tag_merge_helpers():
    import youtube_recorder.gui as gui
    tmap = {"AI 技术": "AI", "AI 投资": "AI", "财报季": "财报", "财报分析": "财报"}
    assert gui._merge_tags(["AI", "AI 技术", "美联储"], tmap) == ["AI", "美联储"]
    assert gui._merge_tags(["AI 投资", "财报季", "财报分析"], tmap) == ["AI", "财报"]
    assert gui._merge_tags([], tmap) == []
    assert gui._merge_tags(["新能源"], {}) == ["新能源"]


def test_channels_export_import_roundtrip():
    import json, io, os, tempfile
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    con = gui._con()
    con.execute("DELETE FROM channels")
    dbm.add_channel(con, "UCtestexport00000000001", "https://www.youtube.com/channel/UCtestexport00000000001", "测试频道A")
    con.execute("UPDATE channels SET grp='投资,科技' WHERE channel_id='UCtestexport00000000001'")
    con.commit(); con.close()
    cli = gui.app.test_client()
    # export
    r = cli.get("/channels/export")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    data = json.loads(r.get_data(as_text=True))
    assert data["kind"] == "channel-subscriptions"
    ch = [c for c in data["channels"] if c["channel_id"] == "UCtestexport00000000001"][0]
    assert ch["groups"] == ["投资", "科技"] and ch["enabled"] is True
    # import: new channel + merge groups into existing
    payload = {"kind": "channel-subscriptions", "version": 1, "channels": [
        {"channel_id": "UCtestimport0000000002", "url": "", "name": "测试频道B",
         "enabled": False, "groups": ["新闻"]},
        {"channel_id": "UCtestexport00000000001", "groups": ["宏观"]},
        {"channel_id": "bogus"},
    ]}
    body = {"_csrf": gui.CSRF,
            "file": (io.BytesIO(json.dumps(payload, ensure_ascii=False).encode()), "subs.json")}
    r = cli.post("/channels/import", data=body, content_type="multipart/form-data")
    assert r.status_code == 302 and "imp=ok" in r.headers["Location"]
    assert "added=1" in r.headers["Location"] and "merged=1" in r.headers["Location"]
    con = gui._con()
    b = con.execute("SELECT * FROM channels WHERE channel_id='UCtestimport0000000002'").fetchone()
    assert b is not None and b["enabled"] == 0 and b["grp"] == "新闻"
    a = con.execute("SELECT grp FROM channels WHERE channel_id='UCtestexport00000000001'").fetchone()
    assert set(gui._grps_of(a["grp"])) == {"投资", "科技", "宏观"}
    assert con.execute("SELECT COUNT(*) n FROM channels WHERE channel_id='bogus'").fetchone()["n"] == 0
    con.close()


def test_group_prompts_feature():
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm, article as art_mod
    from youtube_recorder.config import Config, DEFAULT_CONFIG
    import copy
    # group_prompt_for: labeled, multi-group, missing prompts skipped
    con = gui._con()
    con.execute("DELETE FROM channels WHERE channel_id='UCgp0000000000000000001'")
    dbm.add_channel(con, "UCgp0000000000000000001", "https://x", "组测试")
    con.execute("UPDATE channels SET grp='投资,科技' WHERE channel_id='UCgp0000000000000000001'")
    con.commit()
    cfg = Config(copy.deepcopy(DEFAULT_CONFIG))
    cfg.data["groups"]["prompts"] = {"投资": "偏重政策影响", "新闻": "无关"}
    gp = art_mod.group_prompt_for(cfg, con, "UCgp0000000000000000001")
    assert gp == "【组：投资】偏重政策影响"
    assert art_mod.group_prompt_for(cfg, con, None) == ""
    con.close()
    # digest cache path varies with prompt hash
    p0 = gui._digest_cache_path2("2026-07-21", "投资", "")
    p1 = gui._digest_cache_path2("2026-07-21", "投资", "abcd1234")
    assert p0.name != p1.name and p1.name.startswith("abcd1234__")
    # API page POST saves prompts
    cli = gui.app.test_client()
    r = cli.post("/api", data={"_csrf": gui.CSRF, "form": "gprompts",
                               "gname_0": "投资", "gp_0": "结尾给跟踪建议",
                               "gname_1": "科技", "gp_1": ""})
    assert r.status_code == 200
    import youtube_recorder.config as cfg_mod
    assert (cfg_mod.load().get("groups.prompts") or {}).get("投资") == "结尾给跟踪建议"


def test_tag_merge_interactive():
    import json
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir
    # 决策应用：'' = 独立（移除映射），非空 = 强制归属
    tmap = {"AI 技术": "AI", "AI 芯片": "AI", "财报季": "财报"}
    dec = {"AI 芯片": "", "宏观数据": "宏观"}
    out = gui._apply_tag_decisions(tmap, dec)
    assert "AI 芯片" not in out and out["宏观数据"] == "宏观" and out["AI 技术"] == "AI"
    # answers 端点：写入 decisions 并更新 map
    con = gui._con()
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) VALUES('UCtq','','t',1,?)", (dbm.now(),))
    dbm.upsert_discovered(con, "tqvid01", "UCtq", "v", "2026-07-20T00:00:00Z")
    con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) VALUES('tqvid01','wiki','/tmp/q.md','h',?)", (dbm.now(),))
    con.commit(); con.close()
    wd = work_dir("tqvid01"); wd.mkdir(parents=True, exist_ok=True)
    (wd / "article.json").write_text(json.dumps(
        {"tags": ["AI", "AI 芯片", "芯片"]}, ensure_ascii=False))
    gui._tagmap_path().write_text(json.dumps(
        {"map": {}, "decisions": {}, "tags": ["AI", "AI 芯片", "芯片"]},
        ensure_ascii=False))
    cli = gui.app.test_client()
    r = cli.post("/tags/merge/answers", data={
        "_csrf": gui.CSRF,
        "answers": json.dumps({"AI 芯片": "芯片", "bogus": "AI"})})
    d = r.get_json()
    assert d["ok"] and d["applied"] == 1
    saved = json.loads(gui._tagmap_path().read_text())
    assert saved["decisions"]["AI 芯片"] == "芯片"
    assert saved["map"]["AI 芯片"] == "芯片"


def test_orphan_tag_counts_and_hidden_filter():
    import json
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir
    con = gui._con()
    con.execute("DELETE FROM writes")
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) VALUES('UCorph','','o',1,?)", (dbm.now(),))
    # 3 篇文章：AI 出现 3 次；孤儿标签「稀有A/稀有B」各只 1 篇
    specs = {"o1": ["AI", "稀有A"], "o2": ["AI", "稀有B"], "o3": ["AI"]}
    for vid, tags in specs.items():
        dbm.upsert_discovered(con, vid, "UCorph", vid, "2026-07-20T00:00:00Z")
        con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) VALUES(?,'wiki',?,'h',?)",
                    (vid, f"/tmp/{vid}.md", dbm.now()))
        wd = work_dir(vid); wd.mkdir(parents=True, exist_ok=True)
        (wd / "article.json").write_text(json.dumps({"tags": tags}, ensure_ascii=False))
    con.commit()
    counts = gui._canon_article_counts(con, {})
    assert counts["AI"] == 3 and counts["稀有A"] == 1 and counts["稀有B"] == 1
    con.close()
    # hidden filter hides orphans from reports.json
    gui._tagmap_path().write_text(json.dumps(
        {"map": {}, "decisions": {}, "hidden": ["稀有A", "稀有B"]}, ensure_ascii=False))
    cli = gui.app.test_client()
    data = cli.get("/reports.json").get_json()
    all_tags = {t for r in data for t in (r.get("tags") or [])}
    assert "AI" in all_tags and "稀有A" not in all_tags and "稀有B" not in all_tags
    gui._tagmap_path().unlink(missing_ok=True)


def test_report_tag_remove():
    import json
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir
    import youtube_recorder.config as cfg_mod
    import os
    root = os.path.join(os.environ["YTREC_HOME"], "vault2")
    os.makedirs(root, exist_ok=True)
    note = os.path.join(root, "n.md")
    open(note, "w", encoding="utf-8").write(
        '---\ntype: video\ntags: ["投资", "美股", "英伟达"]\n---\n\n# t\n\n正文\n')
    c = cfg_mod.load(); c.data["vault"]["root"] = root; cfg_mod.save(c)
    con = gui._con()
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) VALUES('UCtr','','t',1,?)", (dbm.now(),))
    dbm.upsert_discovered(con, "trvid", "UCtr", "v", "2026-07-20T00:00:00Z")
    con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) VALUES('trvid','wiki',?,'h',?)", (note, dbm.now()))
    con.commit(); con.close()
    wd = work_dir("trvid"); wd.mkdir(parents=True, exist_ok=True)
    (wd / "article.json").write_text(json.dumps(
        {"tags": ["投资", "美股", "英伟达"]}, ensure_ascii=False))
    cli = gui.app.test_client()
    # reading page shows editable chips
    html = cli.get("/reports/trvid").get_data(as_text=True)
    assert "tgedit" in html and "英伟达" in html
    # delete one tag
    r = cli.post("/reports/trvid/tag-remove", data={"_csrf": gui.CSRF, "tag": "美股"})
    d = r.get_json()
    assert d["ok"] and d["removed"] == 1 and "美股" not in d["tags"]
    # article.json updated
    saved = json.loads((wd / "article.json").read_text())
    assert saved["tags"] == ["投资", "英伟达"]
    # note frontmatter updated
    nt = open(note, encoding="utf-8").read()
    assert '"美股"' not in nt and '"投资"' in nt and '"英伟达"' in nt
    # idempotent
    r2 = cli.post("/reports/trvid/tag-remove", data={"_csrf": gui.CSRF, "tag": "美股"})
    assert r2.get_json()["removed"] == 0
