"""
Fixtures for the live layer: real requests to ListenBrainz and MusicBrainz.

test_live_apis.py and test_live_endpoints.py are READ-ONLY.
test_live_writes.py changes YOUR account (then restores it) and runs only when
ENCORE_WRITE_TESTS=1 is set.

Settings (environment or .env):
    ENCORE_TEST_USER   public ListenBrainz user whose data is read
                       (default: addiboyer24, the course's public demo account)
    LISTENBRAINZ_TOKEN optional; enables the token check and the LB Radio and popularity tests
    LISTENBRAINZ_USER  optional; the token check confirms the token belongs to this user
    ENCORE_WRITE_TESTS set to 1 to run the write round trips on LISTENBRAINZ_USER
    CONTACT_EMAIL      goes in the User-Agent; please set it to your own address

If the APIs can't be reached, every live test is skipped with the reason.
Please don't run this layer during class: MusicBrainz limits requests per IP
address, and the whole class shares one on the campus network.
"""

import os
import time

import pytest
import requests

DEFAULT_TEST_USER = "addiboyer24"


@pytest.fixture(scope="session")
def test_user():
    return os.getenv("ENCORE_TEST_USER", DEFAULT_TEST_USER)


@pytest.fixture(scope="session")
def reachable(agent_module):
    """Skip the whole live layer, with a clear reason, if either API is unreachable."""
    problems = []
    for name, url in [("ListenBrainz", f"{agent_module.LB_API}/validate-token"),
                      ("MusicBrainz", f"{agent_module.MB_API}/genre/all?limit=1&fmt=json")]:
        try:
            requests.get(url, headers={"User-Agent": agent_module.USER_AGENT}, timeout=10)
        except requests.RequestException as exc:
            problems.append(f"{name}: {type(exc).__name__}")
    if problems:
        pytest.skip("APIs not reachable from this machine (" + "; ".join(problems) + ")")
    time.sleep(agent_module.MB_MIN_INTERVAL_S)   # the probe bypassed http(), so keep MusicBrainz spacing
    return True


@pytest.fixture
def live_enc(agent_module, reachable, test_user, monkeypatch):
    """The real agent module, reading the test user's public data."""
    monkeypatch.setattr(agent_module, "LB_USER", test_user)
    return agent_module
