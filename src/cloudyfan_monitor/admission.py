"""One nonblocking service execution slot, shared by threads and workers."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from .store import MonitorError


class Admission:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise ValueError("Safe admission lock path required")

    @contextmanager
    def acquire(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise MonitorError("unsafe_admission_lock", 403)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MonitorError("service_busy", 429) from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
