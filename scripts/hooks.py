"""Install a repo-scoped quick-check hook, chaining the existing secret hook."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def install(root):
    target = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir')) / 'cloudyfan-hooks'
    current = Path(git('rev-parse', '--git-path', 'hooks')).resolve()
    if current == target:
        print('Repository check hook already installed.')
        return 0
    if target.exists():
        print('Managed hook directory already exists; inspect it before changing configuration.', file=sys.stderr)
        return 1
    target.mkdir()
    # Preserve other active hooks, including shared/global hooks. Symlinks
    # observe updates to their contents without copying credential scanners.
    if current.is_dir():
        for hook in current.iterdir():
            if hook.name != 'pre-commit' and hook.is_file():
                (target / hook.name).symlink_to(hook.resolve())
    (target / 'pre-commit').symlink_to(root / '.githooks/pre-commit')
    # Save the effective prior directory, including any inherited global hook.
    git('config', '--local', 'cloudyfan.previousHooksPath', str(current))
    git('config', '--local', 'cloudyfan.previousPreCommitRequired',
        'true' if (current / 'pre-commit').exists() else 'false')
    git('config', '--local', 'core.hooksPath', str(target))
    print(f'Installed repo-scoped check hook; prior hooks preserved at {current}')
    return 0


def run(root):
    try:
        previous = Path(git('config', '--local', '--get', 'cloudyfan.previousHooksPath'))
    except subprocess.CalledProcessError:
        print('Missing previous hook setting; install with python3 scripts/hooks.py install.', file=sys.stderr)
        return 1
    current = Path(git('rev-parse', '--git-path', 'hooks')).resolve()
    if not previous.is_absolute() or previous.resolve() in {current, (root / '.githooks').resolve()}:
        print('Invalid previous hooks directory; refusing recursive or ambiguous execution.', file=sys.stderr)
        return 1
    prior_hook = previous / 'pre-commit'
    required = git('config', '--local', '--get', 'cloudyfan.previousPreCommitRequired') == 'true'
    if required and not prior_hook.is_file():
        print('Previously active pre-commit hook is missing; refusing to skip it.', file=sys.stderr)
        return 1
    if prior_hook.exists():
        if not os.access(prior_hook, os.X_OK):
            print('Previous pre-commit hook is not executable; resolve before committing.', file=sys.stderr)
            return 1
        result = subprocess.run([str(prior_hook)], cwd=root, check=False).returncode
        if result:
            return result
    return subprocess.run([str(root / 'scripts/check'), '--quick'], cwd=root, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'run'])
    args = parser.parse_args()
    root = Path(git('rev-parse', '--show-toplevel')).resolve()
    os.chdir(root)
    return install(root) if args.action == 'install' else run(root)


if __name__ == '__main__':
    sys.exit(main())
