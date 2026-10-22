import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("KIS contract test attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", reject)
