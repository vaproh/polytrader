"""Reference price: the 60-second TWAP, the USD basis, and the strike."""
from __future__ import annotations

import io
import json
import time

import pytest

from polytrader import detector
from polytrader.engine import roll_to_window
from polytrader.feeds import basis
from polytrader.state import S


def test_twap_is_the_mean_of_the_last_minute():
    hist = [(0, 100.0), (30, 110.0), (60, 120.0), (61, 999.0)]
    assert detector.twap60(hist, now=60, min_samples=1) == pytest.approx(110.0)


def test_twap_needs_enough_samples():
    assert detector.twap60([(1, 100.0), (2, 101.0)], now=2, min_samples=30) is None


def test_twap_ignores_ticks_after_the_boundary():
    hist = [(0, 100.0), (59, 100.0), (60, 100.0), (60.1, 1000.0)]
    assert detector.twap60(hist, now=60) == pytest.approx(100.0)


def test_rollover_uses_the_twap_and_the_basis(S):
    S.btc_hist.clear()
    for i in range(60):
        S.btc_hist.append((64.0 + i, 80000.0))
    S.btc = 81000.0
    S.usdt_usd = 0.5

    roll_to_window(123, {"up": "U", "dn": "D"})

    assert S.strike == pytest.approx(40000.0)
    assert S.twap == pytest.approx(40000.0)


def test_rollover_falls_back_to_spot_without_history(S):
    S.btc_hist.clear()
    S.btc = 81000.0

    roll_to_window(123, {"up": "U"})

    assert S.strike == pytest.approx(81000.0)


def test_fair_value_uses_the_twap_not_spot(S):
    now = time.time()
    S.btc_hist.clear()
    for i in range(200):
        S.btc_hist.append((now - 100 + i * 0.5, 80000.0 + (i % 2)))
    S.klines = [80000.0 + (i % 2) for i in range(40)]
    S.strike = 80000.0
    S.twap = 80000.0
    S.btc = 90000.0
    S.ws_start = int(now) - 60

    assert detector.fair_p() == pytest.approx(0.5, abs=0.02)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_basis_is_read_from_kraken(monkeypatch):
    payload = json.dumps({"result": {"USDTZUSD": {"c": ["0.9984", "1"]}}}).encode()
    monkeypatch.setattr(basis.urllib.request, "urlopen", lambda *a, **k: _Resp(payload))

    assert basis.fetch_usdt_usd() == pytest.approx(0.9984)


def test_basis_rejects_an_implausible_rate(monkeypatch):
    payload = json.dumps({"result": {"USDTZUSD": {"c": ["2.0", "1"]}}}).encode()
    monkeypatch.setattr(basis.urllib.request, "urlopen", lambda *a, **k: _Resp(payload))

    assert basis.fetch_usdt_usd() is None


def test_basis_returns_none_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(basis.urllib.request, "urlopen", boom)
    assert basis.fetch_usdt_usd() is None
