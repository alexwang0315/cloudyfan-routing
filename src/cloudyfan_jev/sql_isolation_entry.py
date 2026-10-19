"""Trusted stdlib-only trampoline: join the prechecked leaf before exec bwrap.

No model SQL or response parsing is performed outside the sandbox. The parent
holds an exclusive leaf lease and kills its entire cgroup on all return paths.
"""
import hashlib
import json
import os
from pathlib import Path
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (2,2))
    group, binary_digest, encoded = sys.argv[1:]
    command = json.loads(encoded)
    if hashlib.sha256(Path(command[0]).read_bytes()).hexdigest() != binary_digest:
        raise RuntimeError('Isolation artifact changed')
    with (Path(group)/'cgroup.procs').open('w') as control:
        control.write(str(os.getpid()))
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
