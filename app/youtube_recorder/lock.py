"""Single-process lock. Prevents overlapping runs (long transcriptions can
exceed the 2-hour schedule interval)."""

from __future__ import annotations

import os
import sys
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
        self._fh = open(self.path, "a+b")
        try:
            _lock_exclusive_nonblocking(self._fh)
        except OSError:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(f"another ytrec run holds {self.path}")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()).encode())
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None


# fcntl(flock)/msvcrt(locking) 是 POSIX/Windows 各自的文件锁 API，互不通用，
# 拆成两个小函数按平台选——之前整个模块顶部 `import fcntl` 是硬阻断，
# Windows 上连 import lock 这一步就直接 ModuleNotFoundError，cli.py 又是
# 几乎所有子命令的入口，等于整个程序在 Windows 上一行都跑不起来（这个坑
# 是 Windows CI 第一次真正跑测试套件时才炸出来的，之前的可移植性审计
# 完全没扫到这个文件）。

def _lock_exclusive_nonblocking(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        # msvcrt.locking 锁的是"当前文件位置往后 nbytes 字节"，锁过 EOF
        # 的行为不可靠，所以先保证文件至少有 1 字节可锁。
        if os.fstat(fh.fileno()).st_size == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # 进程都要退出了，解不解锁不影响正确性
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
