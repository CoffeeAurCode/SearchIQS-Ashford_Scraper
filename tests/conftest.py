import os
import socket

import pytest


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def guard(*args, **kwargs):
        raise NetworkBlocked("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("SEARCHIQS_") or name.startswith("GOOGLE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return tmp_path
