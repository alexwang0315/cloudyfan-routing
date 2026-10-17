"""All tests are offline: accidentally selecting a real transport fails closed."""
import socket

import pytest


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network access is forbidden in core tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
