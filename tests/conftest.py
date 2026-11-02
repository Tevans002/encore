"""
Shared fixtures for the Encore test suite.

Finds your agent module (encore.py, or encore_starter.py if you haven't renamed
it yet). The `enc` fixture (unit layer) replaces the network, clock, and
credentials with fakes; the live layer uses `live_enc` in tests/live/conftest.py.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                         # your encore.py
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/helpers.py


def _load_agent_module():
    for name in ("encore", "encore_starter"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise                       # the module exists but one of ITS imports failed
    pytest.exit("Could not find encore.py next to the tests/ folder.", returncode=2)


@pytest.fixture(scope="session")
def agent_module():
    return _load_agent_module()


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeNetwork:
    """Records every request and answers from a queue of FakeResponses (or exceptions)."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json,
                           "headers": headers or {}, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def net(agent_module, monkeypatch):
    fake = FakeNetwork()
    monkeypatch.setattr(agent_module.requests, "request", fake.request)
    return fake


@pytest.fixture
def clock(agent_module, monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(agent_module.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(agent_module.time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def enc(agent_module, net, clock, monkeypatch):
    """The agent module with fake network, fake clock, test credentials, and fresh rate-limit state."""
    monkeypatch.setattr(agent_module, "LB_USER", "test_user")
    monkeypatch.setattr(agent_module, "LB_TOKEN", "SECRET-TOKEN-123")
    monkeypatch.setattr(agent_module, "_last_mb_call", 0.0)
    monkeypatch.setattr(agent_module, "_lb_wait_until", 0.0)
    return agent_module


@pytest.fixture
def FakeResp():
    return FakeResponse
