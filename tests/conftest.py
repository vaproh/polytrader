"""Test fixtures.

HOME is redirected before the package is imported so the terminal's config
file never touches the real one.
"""
from __future__ import annotations

import os
import tempfile

_TMP_HOME = tempfile.mkdtemp(prefix="polytrader-tests-")
os.environ["HOME"] = _TMP_HOME

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """Keep the suite offline even when a real .env exists.

    LiveBroker reads .env on construction, so deleting PK is not enough: the
    loader would put the user's real key back and the suite would sign with it.
    """
    from polytrader import broker

    for key in ("PK", "PRIVATE_KEY", "POLY_FUNDER", "POLY_MAX_USDC"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(broker, "_load_dotenv", lambda: None)


@pytest.fixture
def S(monkeypatch):
    """A clean global state, config pointed at a throwaway file."""
    from polytrader import config, state

    monkeypatch.setattr(config, "CFG_PATH", os.path.join(_TMP_HOME, "cfg.json"))
    state.S.__init__()
    return state.S
