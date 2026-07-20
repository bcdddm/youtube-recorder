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
