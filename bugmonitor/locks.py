"""Cross-process file locks. The OS releases them automatically if a process dies."""
from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Non-blocking exclusive lock. `acquire()` returns False if another process holds it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        if not _try_lock(fh):
            fh.close()
            return False
        fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        self.fh = fh
        return True

    def holder_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.fh:
            _unlock(self.fh)
            self.fh.close()
            self.fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
