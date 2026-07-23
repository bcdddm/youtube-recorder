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
