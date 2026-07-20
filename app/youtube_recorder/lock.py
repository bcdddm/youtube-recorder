"""Single-process lock. Prevents overlapping runs (long transcriptions can
exceed the 2-hour schedule interval)."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .paths import LOCK_FILE


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path = LOCK_FILE):
        self.path = path
        self._fh = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(f"another ytrec run holds {self.path}")
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
