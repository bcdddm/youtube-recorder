"""Regression guard: every <script> on every page must be valid JavaScript.
(0.2.7 shipped a broken ternary that killed the whole queue render — this
test would have caught it.)"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytrec-js-")
os.environ["YTREC_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod      # noqa: E402
from youtube_recorder.paths import ensure_dirs      # noqa: E402

ensure_dirs()
cfg_mod.write_default_if_missing()

from youtube_recorder import gui                    # noqa: E402


def test_all_page_scripts_parse():
    node = shutil.which("node")
    if not node:
        print("  (node not available — skipped)")
        return
    cl = gui.app.test_client()
    for page in ("/channels", "/queue", "/reports", "/settings", "/download"):
        html = cl.get(page).get_data(as_text=True)
        for i, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
            p = Path(_TMP) / f"chk-{page.strip('/')}-{i}.js"
            p.write_text(script, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True)
            assert r.returncode == 0, f"{page} script#{i}: {r.stderr[:300]}"


def test_digest_tools_js():
    """日报页附加脚本必须能通过 node --check（防转义事故复发）。"""
    import re, subprocess, tempfile
    import youtube_recorder.gui as gui
    s = re.search(r"<script>(.*?)</script>", gui._DIGEST_TOOLS_JS, re.S).group(1)
    s = s.replace("__RAWMD__", '"x"')
    p = tempfile.mktemp(suffix=".js")
    open(p, "w").write(s)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr



if __name__ == "__main__":
    test_all_page_scripts_parse()
    print("ok  test_all_page_scripts_parse")
    test_digest_tools_js()
    print("ok  test_digest_tools_js")
    print("all JS syntax tests passed")