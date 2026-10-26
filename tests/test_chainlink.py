"""The exact Chainlink feed must use the window's open and latest TWAP."""
from __future__ import annotations

import pytest

from polytrader.feeds import chainlink


def _serve(monkeypatch, payload):
    monkeypatch.setattr(chainlink, "_get", lambda *a, **k: payload)


def test_fetch_uses_the_first_point_as_the_beat(monkeypatch):
    ws = 1_000_000
    rows = [
        {"timestamp": (ws - 1) * 1000, "value": 90.0},   # previous window
        {"timestamp": ws * 1000, "value": 100.0},        # open
        {"timestamp": (ws + 5) * 1000, "value": 101.0},
        {"timestamp": (ws + 300) * 1000, "value": 102.0},
        {"timestamp": (ws + 301) * 1000, "value": 103.0},  # next window
    ]
    _serve(monkeypatch, rows)

    data = chainlink.fetch(ws)

    assert data["beat"] == pytest.approx(100.0)
    assert data["now"] == pytest.approx(102.0)
    assert data["n"] == 3


def test_fetch_fills_a_late_open_from_the_open_endpoint(monkeypatch):
    ws = 1_000_000
    rows = [{"timestamp": (ws + 5) * 1000, "value": 101.0}]
    _serve(monkeypatch, rows)
    monkeypatch.setattr(chainlink, "fetch_open", lambda *a, **k: 99.5)

    data = chainlink.fetch(ws)

    assert data["beat"] == pytest.approx(99.5)
    assert data["now"] == pytest.approx(101.0)


def test_fetch_returns_none_without_a_usable_point(monkeypatch):
    _serve(monkeypatch, [{"timestamp": None, "value": "x"}])

    assert chainlink.fetch(1_000_000) is None


def test_fetch_open_reads_open_price(monkeypatch):
    _serve(monkeypatch, {"openPrice": 80113.84})

    assert chainlink.fetch_open(1_000_000) == pytest.approx(80113.84)


def test_apply_publishes_only_for_the_current_window(S):
    S.ws_start = 42

    assert chainlink.apply(42, {"beat": 100.0, "now": 101.0, "ts": 1.0})
    assert S.strike == pytest.approx(100.0)
    assert S.twap == pytest.approx(101.0)
    assert S.twap_src == "chainlink"

    S.ws_start = 43
    assert not chainlink.apply(42, {"beat": 1.0, "now": 2.0, "ts": 1.0})
    assert S.strike == pytest.approx(100.0)
