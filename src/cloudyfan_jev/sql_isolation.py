"""Fail-closed Linux worker launcher; target qualification is separate evidence.

Uses an administrator-provisioned, exclusive cgroup v2 leaf and immutable
minimal rootfs. Never creates/mounts/configures a cgroup or downloads an image.
The host supervisor must retain exclusive ownership of the leaf and artifacts.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import time


VERSION = 'sql-linux-isolation-v1'
CGROUP_LIMITS = {'memory.max': '268435456', 'memory.swap.max': '0',
                 'memory.oom.group': '1', 'pids.max': '8', 'cpu.max': '100000 100000',
                 'cgroup.type': 'domain'}
NAMESPACES = ('user', 'ipc', 'pid', 'net', 'uts', 'cgroup')


class IsolationUnavailable(RuntimeError):
    def __init__(self, code='SQL_ISOLATION_UNAVAILABLE'):
        self.code = code
        super().__init__('Strict SQL isolation unavailable')


def tree_digest(root):
    """Hash an already-provisioned rootfs, including modes and symlink targets.

    Mountpoints must be empty and no special files are accepted. Symlinks can
    refer to absolute paths *inside* the sandbox, never to an added host mount.
    Read-only mount enforcement is checked separately before every launch.
    """
    root = Path(root)
    digest = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        info = p.lstat()
        relative = p.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            value = os.readlink(p).encode()
        elif stat.S_ISREG(info.st_mode):
            value = p.read_bytes()
        elif stat.S_ISDIR(info.st_mode):
            value = b''
        else:
            raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        digest.update(relative.encode() + b'\0' + str(info.st_mode).encode() + b'\0' + value + b'\0')
    return digest.hexdigest()


@dataclass(frozen=True)
class LinuxIsolation:
    rootfs: str
    rootfs_sha256: str
    bwrap: str
    bwrap_sha256: str
    cgroup: str
    kernel: str
    machine: str

    def __post_init__(self):
        for name in ('rootfs', 'bwrap', 'cgroup'):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value.startswith('/') or '\0' in value
                    or '..' in Path(value).parts or str(Path(value)) != value or value == '/'):
                raise ValueError('Isolation paths must be explicit absolute normalized paths')
        for name in ('rootfs_sha256', 'bwrap_sha256'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                raise ValueError('Isolation artifacts require SHA256 pins')
        if any(not isinstance(v, str) or not v.strip() for v in (self.kernel, self.machine)):
            raise ValueError('Isolation requires exact kernel and machine identity')

    def descriptor(self):
        folder = Path(__file__).parent
        try:
            info = Path(self.cgroup).stat()
            identity = [info.st_dev, info.st_ino]
        except OSError:
            identity = None
        return {'version': VERSION, 'configuration': asdict(self),
                'cgroup_identity': identity,
                'cgroup_limits': dict(CGROUP_LIMITS), 'namespaces': list(NAMESPACES),
                'implementation_sha256': {n: hashlib.sha256((folder/n).read_bytes()).hexdigest()
                                          for n in ('sql_isolation.py','sql_isolation_entry.py')}}

    def _artifacts(self):
        if sys.platform != 'linux' or platform.release() != self.kernel or platform.machine() != self.machine:
            raise IsolationUnavailable('SQL_ISOLATION_PLATFORM')
        root, binary, group = (Path(v) for v in (self.rootfs, self.bwrap, self.cgroup))
        if any(p.resolve(strict=True) != p for p in (root,binary,group)):
            raise IsolationUnavailable('SQL_ISOLATION_PATH')
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before, after = line.split(' - ', 1)
            fields = before.split()
            mounts.append((Path(fields[4]), after.split()[0]))
        containing = [(p,kind) for p,kind in mounts if group.is_relative_to(p)]
        mount, kind = max(containing, key=lambda pair: len(str(pair[0])), default=(None,None))
        if kind != 'cgroup2' or group == mount:
            raise IsolationUnavailable('SQL_ISOLATION_CGROUP')
        if any(p != root and p.is_relative_to(root) for p,kind in mounts):
            raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        if not os.statvfs(root).f_flag & os.ST_RDONLY or tree_digest(root) != self.rootfs_sha256:
            raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        # A host-controlled binary, pinned separately from the rootfs bundle.
        info = binary.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not os.access(binary, os.X_OK)
                or hashlib.sha256(binary.read_bytes()).hexdigest() != self.bwrap_sha256):
            raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        for target in ('proc','dev','tmp'):
            path = root/target
            if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
                raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        for name in ('sql_worker.py','sql_executor.py'):
            path = root/'app'/name
            if path.is_symlink() or path.read_bytes() != (Path(__file__).parent/name).read_bytes():
                raise IsolationUnavailable('SQL_ISOLATION_ARTIFACT')
        return group

    def command(self):
        """Fixed argument shape, never extra caller/model-supplied bwrap flags."""
        args = [self.bwrap]
        for name in NAMESPACES:
            args.append('--unshare-' + name)
        return args + ['--disable-userns', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
                       '--ro-bind', self.rootfs, '/', '--proc', '/proc', '--dev', '/dev',
                       '--size', '8388608', '--tmpfs', '/tmp', '--clearenv', '--chdir', '/app',
                       '/runtime/bin/python3', '-I', '-S', '-B', '/app/sql_worker.py']

    @contextmanager
    def lease(self):
        """Serialize the exclusive pre-existing leaf, contain and reap all exits."""
        fd = None
        entered = False
        try:
            group = self._artifacts()
            fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def read(name):
                control = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    return os.read(control, 8192).decode().strip()
                finally:
                    os.close(control)

            def check_limits():
                if any(read(k) != v for k,v in CGROUP_LIMITS.items()):
                    raise IsolationUnavailable('SQL_ISOLATION_LIMIT_CHANGED')

            check_limits()
            if read('cgroup.procs') or 'populated 0' not in read('cgroup.events').splitlines():
                raise IsolationUnavailable('SQL_ISOLATION_BUSY')
            # Open kill capability BEFORE launching; no late discovery on timeout.
            kill_fd = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                entered = True
                entry = Path(__file__).with_name('sql_isolation_entry.py')
                yield [sys.executable, '-I', '-S', '-B', str(entry), self.cgroup,
                       self.bwrap_sha256, json.dumps(self.command(), separators=(',',':'))]
                check_limits()
            finally:
                # Only this exclusive, previously empty leaf; no directory deletion.
                try:
                    if entered:
                        os.write(kill_fd, b'1')
                        until = time.monotonic() + 1
                        while 'populated 0' not in read('cgroup.events').splitlines():
                            if time.monotonic() >= until:
                                raise IsolationUnavailable('SQL_ISOLATION_REAP')
                            time.sleep(.01)
                finally:
                    os.close(kill_fd)
        except IsolationUnavailable:
            raise
        except (OSError, ValueError, TypeError):
            raise IsolationUnavailable() from None
        finally:
            if fd is not None:
                os.close(fd)
