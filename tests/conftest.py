import os
import socket
import pytest

# git exports these to hooks; inherited, every test git command would act on the real repo.
for _name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY'):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline monitor verification must not open a network connection")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
