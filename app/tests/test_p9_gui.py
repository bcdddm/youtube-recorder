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


def test_digest_bulk_tag_remove():
    import json, os
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder.paths import work_dir
    import youtube_recorder.config as cfg_mod
    root = os.path.join(os.environ["YTREC_HOME"], "vaultd")
    os.makedirs(root, exist_ok=True)
    c = cfg_mod.load(); c.data["vault"]["root"] = root; cfg_mod.save(c)
    con = gui._con()
    con.execute("DELETE FROM writes")
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) VALUES('UCbd','','b',1,?)", (dbm.now(),))
    for vid, tags in {"bd1": ["AI", "美股"], "bd2": ["AI", "财报"]}.items():
        dbm.upsert_discovered(con, vid, "UCbd", vid, "2026-07-20T08:00:00Z")
        con.execute("UPDATE videos SET published_at='2026-07-20T08:00:00Z' WHERE video_id=?", (vid,))
        note = os.path.join(root, vid + ".md")
        open(note, "w", encoding="utf-8").write('---\ntags: ["AI"]\n---\n# t\n')
        con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) VALUES(?,'wiki',?,'h',?)", (vid, note, dbm.now()))
        wd = work_dir(vid); wd.mkdir(parents=True, exist_ok=True)
        (wd / "article.json").write_text(json.dumps({"tags": tags}, ensure_ascii=False))
    con.commit(); con.close()
    cli = gui.app.test_client()
    r = cli.post("/reports/digest/tag-remove", data={
        "_csrf": gui.CSRF, "date": "2026-07-20", "grp": "", "tag": "AI"})
    d = r.get_json()
    assert d["ok"] and d["removed"] == 2 and d["articles"] == 2
    assert json.loads((work_dir("bd1") / "article.json").read_text())["tags"] == ["美股"]
    assert json.loads((work_dir("bd2") / "article.json").read_text())["tags"] == ["财报"]
    # idempotent second call
    d2 = cli.post("/reports/digest/tag-remove", data={
        "_csrf": gui.CSRF, "date": "2026-07-20", "grp": "", "tag": "AI"}).get_json()
    assert d2["removed"] == 0


def test_quickdl_valid_url_and_job_lifecycle():
    from youtube_recorder import quickdl
    assert quickdl.valid_url("https://youtu.be/abc123")
    assert quickdl.valid_url("http://x.com/v")
    assert not quickdl.valid_url("not a url")
    assert not quickdl.valid_url("")
    assert not quickdl.valid_url("ftp://x.com")

    jid = quickdl._new_job()
    j = quickdl.get_job(jid)
    assert j["status"] == "queued" and j["id"] == jid
    with quickdl._LOCK:
        quickdl._JOBS[jid]["status"] = "done"
        quickdl._JOBS[jid]["pct"] = 100.0
    jobs = quickdl.list_jobs()
    assert any(x["id"] == jid for x in jobs)
    with quickdl._LOCK:
        quickdl._JOBS.pop(jid, None)


def test_quickdl_job_cap():
    from youtube_recorder import quickdl
    with quickdl._LOCK:
        quickdl._JOBS.clear()
    ids = []
    for i in range(quickdl.MAX_JOBS + 5):
        jid = quickdl._new_job()
        with quickdl._LOCK:
            quickdl._JOBS[jid]["status"] = "done"
        ids.append(jid)
    assert len(quickdl._JOBS) <= quickdl.MAX_JOBS
    with quickdl._LOCK:
        quickdl._JOBS.clear()


def test_download_page_routes():
    import youtube_recorder.gui as gui
    cli = gui.app.test_client()
    r = cli.get("/download")
    assert r.status_code == 200 and "粘贴链接下载视频" in r.get_data(as_text=True)
    r2 = cli.post("/download", data={"_csrf": gui.CSRF, "url": "not-a-url", "quality": "1080p"})
    assert "请粘贴完整" in r2.get_data(as_text=True)
    r3 = cli.get("/download/jobs.json")
    assert r3.status_code == 200 and isinstance(r3.get_json(), list)
    # path traversal guard on reveal
    r4 = cli.post("/download/reveal", data={"_csrf": gui.CSRF, "path": "/etc/passwd"})
    assert r4.status_code == 400


def test_friendly_error_classification():
    from youtube_recorder import quickdl
    # 已知模式命中 -> 具体中文原因
    assert "不可用" in quickdl._friendly_error("ERROR: Video unavailable", True)
    assert "私享" in quickdl._friendly_error("This video is private", True)
    assert "年龄限制" in quickdl._friendly_error("Sign in to confirm your age", True)
    assert "地区" in quickdl._friendly_error("The uploader has not made this video available in your country, blocked it in your country", True)
    assert "清晰度" in quickdl._friendly_error("Requested format is not available", True)
    assert "ffmpeg" in quickdl._friendly_error("ffmpeg not found on path", True)
    assert "网络" in quickdl._friendly_error("Connection timed out", True)
    # 未识别模式 -> 按 parsed_ok 前缀区分"解析失败"/"下载失败"
    parse_msg = quickdl._friendly_error("some totally unknown yt-dlp internal error xyz", False)
    assert parse_msg.startswith("解析失败：")
    dl_msg = quickdl._friendly_error("some totally unknown yt-dlp internal error xyz", True)
    assert dl_msg.startswith("下载失败：")


def test_settings_downloads_form_relocated():
    import youtube_recorder.gui as gui
    cli = gui.app.test_client()
    dl_html = cli.get("/download").get_data(as_text=True)
    # 下载页不再直接托管保存位置/清晰度表单，只留一条到设置页的说明链接
    assert 'name=form value=settings' not in dl_html
    assert '/settings#downloads' in dl_html

    st_html = cli.get("/settings").get_data(as_text=True)
    assert 'id=downloads' in st_html
    assert 'name=form value=downloads' in st_html

    r = cli.post("/settings", data={
        "_csrf": gui.CSRF, "form": "downloads",
        "dest_dir": "/tmp/ytrec-download-test", "default_quality": "720p",
    })
    assert r.status_code == 200
    from youtube_recorder import config as cfg_mod
    cfg = cfg_mod.load()
    assert cfg.get("downloads.dest_dir") == "/tmp/ytrec-download-test"
    assert cfg.get("downloads.default_quality") == "720p"


def test_maybe_autogenerate_digest():
    """当天（全部组）文章数达到 3 篇才自动后台生成日报；未新增内容时
    不重复调用 AI；有新增内容达到阈值后会用 force 刷新缓存。"""
    import json, os
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder import providers
    from youtube_recorder.paths import work_dir

    root = os.path.join(os.environ["YTREC_HOME"], "vault-auto")
    os.makedirs(root, exist_ok=True)
    c = cfg_mod.load(); c.data["vault"]["root"] = root; cfg_mod.save(c)

    today = dbm.local_date(dbm.now())
    con = gui._con()
    con.execute("DELETE FROM writes")
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
                "VALUES('UCauto','','auto',1,?)", (dbm.now(),))

    def _seed(vid):
        dbm.upsert_discovered(con, vid, "UCauto", vid, dbm.now())
        con.execute("UPDATE videos SET published_at=? WHERE video_id=?", (dbm.now(), vid))
        note = os.path.join(root, vid + ".md")
        open(note, "w", encoding="utf-8").write('---\ntags: []\n---\n# t\n正文内容。\n')
        con.execute("INSERT INTO writes(video_id,note_kind,note_path,content_hash,at) "
                    "VALUES(?,'wiki',?,'h',?)", (vid, note, dbm.now()))
        wd = work_dir(vid); wd.mkdir(parents=True, exist_ok=True)
        (wd / "article.json").write_text(json.dumps(
            {"title_zh": vid, "tags": [], "summary": "s", "sections": []}, ensure_ascii=False))

    # 只有 2 篇时不应触发
    _seed("auto1"); _seed("auto2")
    con.commit(); con.close()

    calls = []

    def fake_complete_long(cfg, con_, vid, sys_, user_, **kw):
        calls.append(1)
        return "生成的日报正文。"

    orig = providers.complete_long
    providers.complete_long = fake_complete_long
    try:
        cache = gui._digest_cache_path(today, "")
        cache.unlink(missing_ok=True)
        state_path = gui._digest_auto_state_path()
        state_path.unlink(missing_ok=True)

        t = gui.maybe_autogenerate_digest()
        t.join(timeout=10)
        assert len(calls) == 0, "只有 2 篇不该触发自动生成"
        assert not cache.exists()

        # 第 3 篇到达，跨过阈值 -> 应该自动生成
        con = gui._con()
        _seed("auto3")
        con.commit(); con.close()

        t = gui.maybe_autogenerate_digest()
        t.join(timeout=10)
        assert len(calls) == 1, "满 3 篇应触发一次自动生成"
        assert cache.exists()
        assert cache.read_text(encoding="utf-8") == "生成的日报正文。"

        # 没有新增内容 -> 不应重复生成
        t = gui.maybe_autogenerate_digest()
        t.join(timeout=10)
        assert len(calls) == 1, "内容没变化不该重复调用 AI"

        # 第 4 篇到达 -> 应刷新缓存
        con = gui._con()
        _seed("auto4")
        con.commit(); con.close()

        def fake_complete_long2(cfg, con_, vid, sys_, user_, **kw):
            calls.append(1)
            return "刷新后的日报正文。"
        providers.complete_long = fake_complete_long2

        t = gui.maybe_autogenerate_digest()
        t.join(timeout=10)
        assert len(calls) == 2, "有新增内容应重新生成一次"
        assert cache.read_text(encoding="utf-8") == "刷新后的日报正文。"
    finally:
        providers.complete_long = orig


def test_queue_unskip_ignored_video():
    """已跳过（ignored）的视频在 Queue 页可以"取消跳过"重新执行——
    对应用户需求：序列中被 skip 掉的内容也要能点回来、执行回来。
    取消跳过和 approve 一样会重定向去触发一次立即运行（而不是等下一次
    排班），避免用户点了却看着它一直"卡在第一步"没反应。"""
    import youtube_recorder.gui as gui
    from youtube_recorder import db as dbm
    from youtube_recorder import state as st

    con = gui._con()
    con.execute("INSERT OR IGNORE INTO channels(channel_id,url,name,enabled,added_at) "
                "VALUES('UCunskip','','u',1,?)", (dbm.now(),))
    dbm.upsert_discovered(con, "unskip1", "UCunskip", "t", None)
    dbm.set_status(con, "unskip1", st.IGNORED, error_code="user_skip")
    con.commit(); con.close()

    cli = gui.app.test_client()
    # 不 follow_redirects：run_now_get() 会真的 subprocess.Popen 拉起 CLI，
    # 单测里只需确认它确实触发了重定向，不需要真的跑一遍管线。
    r = cli.post("/queue", data={"_csrf": gui.CSRF, "retry": "unskip1"})
    assert r.status_code == 302
    assert "run-now-redirect" in r.headers.get("Location", "")

    con = gui._con()
    v = dbm.get_video(con, "unskip1")
    con.close()
    assert v["status"] == st.DISCOVERED
