"""Filesystem-only durable call storage shared by separate transport adapters.

No endpoint, credential, billing interpretation or reconciliation lives here.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

_CID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


def _encoded(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _read(path):
    return json.loads(Path(path).read_text())


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _immutable(path, obj):
    """Exclusive create plus data/directory fsync; torn files fail closed."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(_encoded(obj))
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_dir(path.parent)


class ReceiptStore:
    """Private storage and advisory serialization; transport semantics stay separate."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / 'calls').mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def _lock(self):
        with (self.root / ".client.lock").open("a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _directory(self, cid):
        if not isinstance(cid, str) or not _CID.fullmatch(cid) or cid in {".", ".."}:
            raise ValueError("Invalid call ID")
        directory = self.root / "calls" / cid
        if directory.is_symlink():
            raise ValueError("Call directory cannot be a symlink")
        return directory

    def _load(self, directory):
        return _read(directory / 'response.json')
