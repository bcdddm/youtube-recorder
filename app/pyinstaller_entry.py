"""PyInstaller entry point for YouTube Recorder · By Leoluchino.

Thin wrapper around youtube_recorder.cli.main so PyInstaller has a single
concrete script to analyze. Bundling the interpreter + all dependencies
into the .app means macOS never falls back to treating the process as
belonging to the system Python.framework (which used to make the Dock
briefly show the generic Python rocket icon before settling on our own —
see winapp.py's _promote_to_regular_app() and tray.py's open_win()).
"""

import sys

from youtube_recorder.cli import main

if __name__ == "__main__":
    sys.exit(main())
