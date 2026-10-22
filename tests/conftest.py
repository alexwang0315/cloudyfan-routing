import socket
import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline monitor verification must not open a network connection")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
