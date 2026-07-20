"""launchd plist generation from the user's hour schedule (GUI 排班表)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import LAUNCHD_LABEL

PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
APP_DIR = Path(__file__).resolve().parents[1]

_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/arch</string><string>-arm64</string>
        <string>{python}</string><string>-m</string>
        <string>youtube_recorder.cli</string><string>run</string><string>--once</string>
    </array>
    <key>WorkingDirectory</key><string>{workdir}</string>
    <key>StartCalendarInterval</key>
    <array>
{intervals}
    </array>
    <key>RunAtLoad</key><{run_at_load}/>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
    <key>StandardOutPath</key><string>{logdir}/launchd-out.log</string>
    <key>StandardErrorPath</key><string>{logdir}/launchd-err.log</string>
</dict>
</plist>
"""


def render_plist(hours: list[int], run_at_load: bool = True,
                 python: str | None = None) -> str:
    from .paths import LOG_DIR
    ivs = "\n".join(
        f"        <dict><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>0</integer></dict>"
        for h in sorted(set(int(h) % 24 for h in hours)))
    return _TEMPLATE.format(
        label=LAUNCHD_LABEL, python=python or sys.executable,
        workdir=str(APP_DIR), intervals=ivs,
        run_at_load="true" if run_at_load else "false",
        logdir=str(LOG_DIR))


def install(hours: list[int], run_at_load: bool = True) -> str:
    """Write plist + reload. Returns human status line."""
    if not hours:
        raise ValueError("schedule needs at least one hour")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(render_plist(hours, run_at_load), encoding="utf-8")
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"plist written, reload failed: {r.stderr.strip()[:120]}"
    return f"scheduled at hours {sorted(set(hours))}"
