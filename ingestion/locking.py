"""Prevent overlapping ingestion runs via a lock file with stale-PID detection."""

import logging
import os

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class IngestionLock:
    """Acquire by exclusive file creation; a stale lock (dead PID) is broken."""

    def __init__(self, path):
        self.path = str(path)
        self._acquired = False

    def acquire(self) -> bool:
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                self._acquired = True
                return True
            except FileExistsError:
                pass
            try:
                with open(self.path, encoding="utf-8") as fh:
                    raw = fh.read().strip()
                pid = int(raw) if raw.isdigit() else 0
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                return False
            log.warning("Breaking stale ingestion lock at %s (pid %s not alive)", self.path, pid)
            try:
                os.remove(self.path)
            except OSError:
                return False

    def release(self):
        if self._acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self._acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()
        return False
