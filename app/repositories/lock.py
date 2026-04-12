"""fcntl-based advisory lock used by sync and write commands.

Held for the duration of any operation that mutates the local store. Read
commands do not take the lock — SQLite WAL mode handles read concurrency.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.repositories.errors import LockTimeoutError


@contextmanager
def acquire_lock(lock_path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Acquire an exclusive flock on `lock_path`.

    Waits up to `timeout` seconds for contention to clear, polling every 200 ms.
    Raises LockTimeoutError if the lock cannot be obtained.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Another kasten process is holding {lock_path}. Waited {timeout:.0f}s."
                    ) from exc
                time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
