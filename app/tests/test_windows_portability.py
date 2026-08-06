"""Windows-portability regression tests: platform-branching logic added so
the app runs on Windows without macOS-only pieces (AppleScript dialogs,
Finder reveal, launchd scheduling, hardcoded /Applications paths, MacWhisper
watchfolder) crashing or silently misbehaving. Run: python3 -m pytest tests/ -q

None of these need to actually run on Windows to be meaningful — each one
monkeypatches sys.platform/sys.frozen for the duration of a single call and
asserts the branch taken, so the same suite proves the logic correct
regardless of which OS runs pytest (including the real Windows CI runner,
where these should all still pass unchanged).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="ytrec-winport-")
os.environ["YTREC_HOME"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[0] / ".."))

from youtube_recorder import config as cfg_mod          # noqa: E402
from youtube_recorder import paths                       # noqa: E402
from youtube_recorder import pipeline                     # noqa: E402
from youtube_recorder.logging_setup import RunLogger      # noqa: E402
from youtube_recorder.paths import ensure_dirs            # noqa: E402

ensure_dirs()
cfg_mod.write_default_if_missing()


# --- paths.py: per-platform app-data directory ---------------------------------

def test_default_app_support_windows_uses_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\leo\AppData\Local")
    p = paths._default_app_support()
    assert p == Path(r"C:\Users\leo\AppData\Local") / "YouTube Recorder"


def test_default_app_support_windows_falls_back_without_env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    p = paths._default_app_support()
    assert p == Path.home() / "AppData" / "Local" / "YouTube Recorder"


def test_default_app_support_linux_uses_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    p = paths._default_app_support()
    assert p == Path.home() / ".local" / "share" / "YouTube Recorder"


def test_default_app_support_macos_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = paths._default_app_support()
    assert p == Path.home() / "Library" / "Application Support" / "YouTube Recorder"


def test_ensure_dirs_tolerates_chmod_not_really_working(monkeypatch):
    """Windows' os.chmod only toggles the read-only bit — it never raises for
    0o700, so this mostly guards the try/except staying harmless if some
    future platform's chmod *does* raise."""
    real_chmod = os.chmod

    def _boom(path, mode):
        if str(path) == str(paths.APP_SUPPORT):
            raise OSError("pretend this platform's chmod rejects the mode")
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _boom)
    ensure_dirs()  # must not raise


# --- paths.py: launching a background subprocess without a hardcoded --------
# --- macOS-only /Applications path -------------------------------------------

def test_py_cmd_arch_prefix_only_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.py_cmd()[:2] == ["/usr/bin/arch", "-arm64"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.py_cmd() == [sys.executable]


def test_installed_frozen_exe_only_known_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths._installed_frozen_exe() is not None
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths._installed_frozen_exe() is None


def test_cli_launch_argv_reuses_sys_executable_when_frozen(monkeypatch):
    """Works identically on macOS and Windows: PyInstaller sets sys.frozen
    and points sys.executable at the real running binary on both, so this
    needs no platform branch at all — that's the point of the refactor."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        r"C:\Program Files\YouTube Recorder\YouTube Recorder.exe")
    argv, cwd = paths.cli_launch_argv("run", "--once")
    assert argv == [r"C:\Program Files\YouTube Recorder\YouTube Recorder.exe",
                    "run", "--once"]
    assert cwd is None


def test_cli_launch_argv_dev_fallback_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    argv, cwd = paths.cli_launch_argv("run", "--once", "--headless")
    assert argv == [sys.executable, "-m", "youtube_recorder.cli",
                    "run", "--once", "--headless"]
    assert cwd is not None  # dev source root, since not frozen


def test_cli_launch_argv_prefers_installed_app_on_macos_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch.object(Path, "exists", return_value=True):
        argv, cwd = paths.cli_launch_argv("run", "--once")
    assert argv[0] == str(paths._installed_frozen_exe())
    assert cwd is None


# --- pipeline.py: confirm_dialog degrades instead of crashing on non-macOS --

def _cfg_with(scheduler: dict):
    cfg = cfg_mod.load()
    cfg.data.setdefault("scheduler", {}).update(scheduler)
    return cfg


def test_confirm_dialog_skips_applescript_on_non_darwin_and_defaults_to_run(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = _cfg_with({"confirm_dialog": "always", "on_dialog_error": "run"})
    log = RunLogger()
    with patch("subprocess.run") as run:
        assert pipeline.confirm_dialog(cfg, log, n_new=3) is True
        run.assert_not_called()


def test_confirm_dialog_respects_on_dialog_error_skip_on_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = _cfg_with({"confirm_dialog": "always", "on_dialog_error": "skip"})
    log = RunLogger()
    assert pipeline.confirm_dialog(cfg, log, n_new=3) is False


def test_confirm_dialog_still_uses_applescript_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _cfg_with({"confirm_dialog": "always", "on_dialog_error": "run"})
    log = RunLogger()
    with patch("subprocess.run") as run:
        run.return_value.stdout = "button returned:立即处理"
        pipeline.confirm_dialog(cfg, log, n_new=2)
        assert run.called
        assert run.call_args.args[0][0] == "osascript"


# --- config.py: MacWhisper-less default on non-macOS -------------------------

def test_default_transcriber_platform_awareness():
    assert cfg_mod._default_transcriber("darwin") == "macwhisper_watch_srt"
    assert cfg_mod._default_transcriber("win32") == "openai_audio"
    assert cfg_mod._default_transcriber("linux") == "openai_audio"


# --- gui.py: reveal-in-file-manager picks the right command per platform ----

def test_download_reveal_uses_explorer_on_windows(monkeypatch):
    from youtube_recorder import gui
    dest = Path(_TMP) / "dl-win"
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / "video.mp4"
    f.write_bytes(b"x")
    cfg = cfg_mod.load()
    cfg.data.setdefault("downloads", {})["dest_dir"] = str(dest)
    cfg_mod.save(cfg)

    monkeypatch.setattr(sys, "platform", "win32")
    client = gui.app.test_client()
    with patch("subprocess.run") as run:
        r = client.post("/download/reveal",
                        data={"_csrf": gui.CSRF, "path": str(f)})
        assert r.status_code == 204
        # the route does Path(path).resolve() before shelling out, which on
        # Windows can normalize an 8.3 short-name temp path (e.g.
        # RUNNER~1) to its long-name form — compare against the same
        # resolved value rather than the raw string we posted.
        run.assert_called_once_with(["explorer", "/select,", str(f.resolve())])


def test_download_reveal_uses_open_dash_r_on_macos(monkeypatch):
    from youtube_recorder import gui
    dest = Path(_TMP) / "dl-mac"
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / "video.mp4"
    f.write_bytes(b"x")
    cfg = cfg_mod.load()
    cfg.data.setdefault("downloads", {})["dest_dir"] = str(dest)
    cfg_mod.save(cfg)

    monkeypatch.setattr(sys, "platform", "darwin")
    client = gui.app.test_client()
    with patch("subprocess.run") as run:
        r = client.post("/download/reveal",
                        data={"_csrf": gui.CSRF, "path": str(f)})
        assert r.status_code == 204
        run.assert_called_once_with(["open", "-R", str(f.resolve())])


# --- lock.py: fcntl was a hard import-time crash on Windows -----------------

def test_lock_uses_msvcrt_on_windows_and_writes_pid(monkeypatch):
    """fcntl doesn't exist on Windows at all — the old top-level `import
    fcntl` meant `import youtube_recorder.lock` (and therefore cli.py, which
    every subcommand goes through) crashed immediately on Windows. Caught by
    the real Windows CI runner, not by anything in this file originally —
    added here after the fact so the dispatch logic has real coverage."""
    import types
    from youtube_recorder import lock as lock_mod

    calls = []
    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2,
        locking=lambda fd, mode, n: calls.append((mode, n)))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")

    lock_path = Path(_TMP) / "win-test.lock"
    with lock_mod.ProcessLock(lock_path):
        assert (1, 1) in calls  # LK_NBLCK acquire, 1 byte
    assert (2, 1) in calls      # LK_UNLCK release, same byte range
    # __enter__ truncates back to 0 and writes the real pid after locking,
    # so the placeholder null byte written just to give msvcrt something to
    # lock doesn't linger in the final file content.
    assert lock_path.read_bytes() == str(os.getpid()).encode()


def test_lock_still_uses_fcntl_on_posix(monkeypatch):
    """Mirrors the msvcrt test above: fcntl genuinely doesn't exist on the
    Windows CI runner, so — just like msvcrt on macOS/Linux — it has to be
    faked in sys.modules rather than assumed present, even though we're only
    pretending to be on darwin via sys.platform. Without this, this test
    itself was the thing crashing with ModuleNotFoundError on Windows CI,
    not the code it was meant to verify."""
    import types
    monkeypatch.setattr(sys, "platform", "darwin")

    calls = []
    fake_fcntl = types.SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=4,
        flock=lambda fh, op: calls.append(op))
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    from youtube_recorder import lock as lock_mod
    lock_path = Path(_TMP) / "posix-test.lock"
    with lock_mod.ProcessLock(lock_path):
        assert (1 | 2) in calls  # LOCK_EX | LOCK_NB
        assert lock_path.read_bytes() == str(os.getpid()).encode()
    assert 4 in calls  # LOCK_UN on exit


# --- openai_audio.py: ffmpeg lookup no longer assumes Homebrew's mac path --

def test_ffmpeg_fallback_no_longer_hardcodes_homebrew_path(monkeypatch):
    """Regression guard: FFMPEG used to fall back to a hardcoded macOS
    Homebrew path when `which` found nothing, which is wrong on every other
    platform (and wrong on macOS installs that used a different package
    manager too). Simulate "not found" and check the fallback is neutral."""
    import importlib
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    from youtube_recorder import openai_audio as oa
    importlib.reload(oa)
    try:
        assert oa.FFMPEG == "ffmpeg"
    finally:
        importlib.reload(oa)  # restore the real shutil.which-resolved value


# --- cli.py: default double-click launch on a frozen build -----------------

def test_frozen_no_args_launch_defaults_to_app_on_windows(monkeypatch):
    from youtube_recorder import cli, tray, winapp
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    with patch.object(winapp, "main", return_value=0) as win_main, \
         patch.object(tray, "main", return_value=0) as tray_main:
        assert cli.main([]) == 0
    win_main.assert_called_once()
    tray_main.assert_not_called()


def test_frozen_no_args_launch_defaults_to_tray_on_macos(monkeypatch):
    from youtube_recorder import cli, tray, winapp
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch.object(winapp, "main", return_value=0) as win_main, \
         patch.object(tray, "main", return_value=0) as tray_main:
        assert cli.main([]) == 0
    tray_main.assert_called_once()
    win_main.assert_not_called()
