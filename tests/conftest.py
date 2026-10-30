"""All tests are offline: accidentally selecting a real transport fails closed."""
import os
import socket

import pytest

# git exports these to hooks; inherited, every test git command would act on the real repo.
for _name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY'):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network access is forbidden in core tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
