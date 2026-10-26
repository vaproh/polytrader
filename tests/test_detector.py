"""Detector, volatility, fair value and warnings: the numbers the panels show."""
from __future__ import annotations

import pytest

import trader
from trader import BASE_RATES


def feed(regime_mids):
    """Fill the 250ms mid series so the detector sees a full 36s span."""
    trader.S.mid_hist.clear()
    for i, m in enumerate(regime_mids):
        trader.S.mid_hist.append((i, m))


def trend(n=160, start=0.50, step=0.001):
    return [start + i * step for i in range(n)]


def test_fewer_than_four_buckets_is_warmup():
    feed([0.5] * 3)
    trader.recompute_detector()
    assert trader.S.regime == "WARMUP"


def test_a_short_span_is_still_warmup():
    feed([0.5 + i * 0.001 for i in range(40)])  # 10s of history
    trader.recompute_detector()
    assert trader.S.regime == "WARMUP"
    assert trader.S.rates == {}


def test_flat_tape_is_dead():
    feed([0.50] * 160)
    trader.recompute_detector()
    assert trader.S.regime == "DEAD"
    assert trader.S.rates == BASE_RATES["DEAD"]


def test_straight_line_is_trend():
    feed(trend())
    trader.recompute_detector()
    assert trader.S.regime == "TREND"
    assert trader.S.metrics["eff"] == pytest.approx(1.0, abs=1e-6)
    assert trader.S.metrics["revs"] == 0
    assert trader.S.metrics["travel"] == pytest.approx(0.144, abs=1e-6)


def test_choppy_up_is_chop():
    # wide oscillation with a tiny positive net: low efficiency, net > 0
    mids = [0.50 + (0.01 if i % 2 else 0.0) + i * 0.0001 for i in range(160)]
    feed(mids)
    trader.recompute_detector()
    assert trader.S.regime == "CHOP"
    assert trader.S.metrics["net"] > 0
    assert trader.S.metrics["eff"] < 0.35


def test_choppy_down_is_chop_down():
    mids = [0.60 - (0.01 if i % 2 else 0.0) - i * 0.0001 for i in range(160)]
    feed(mids)
    trader.recompute_detector()
    assert trader.S.regime == "CHOP-DOWN"
    assert trader.S.metrics["net"] < 0


def test_violent_tape_is_volatile():
    # up 0.006 / down 0.002: travel 0.64, net 0.32, eff 0.5
    mids, p = [], 0.50
    for i in range(160):
        p += 0.006 if i % 2 else -0.002
        mids.append(p)
    feed(mids)
    trader.recompute_detector()
    assert trader.S.regime == "VOLATILE"
    assert trader.S.metrics["travel"] >= 0.40
    assert 0.35 <= trader.S.metrics["eff"] < 0.70


def test_drifting_quiet_tape_is_quiet_up():
    mids, p = [], 0.50
    for i in range(160):
        p += 0.003 if i % 2 else -0.001
        mids.append(p)
    feed(mids)
    trader.recompute_detector()
    assert trader.S.regime == "QUIET-UP"
    assert trader.S.metrics["travel"] < 0.40
    assert trader.S.metrics["net"] > 0


def test_drift_is_net_over_the_first_kept_bucket():
    feed(trend())
    trader.recompute_detector()
    m = trader.S.metrics
    # the 36s window starts at bucket 15, so mid[0] is 0.515
    assert m["drift"] == pytest.approx(m["net"] / 0.515, abs=1e-9)


def test_rsi_is_100_on_a_monotonic_rise_and_50_while_short():
    assert trader.rsi_last([1, 2, 3]) == 50.0
    assert trader.rsi_last([float(i) for i in range(30)]) == pytest.approx(100.0)


def test_btc_sigma_ignores_a_dead_second_and_uses_the_floor(S):
    # 60 seconds of ticks with a real range
    trader.S.btc_hist.clear()
    for i in range(60):
        trader.S.btc_hist.append((1000.0 + i, 80000.0 + (i % 2) * 3.0))
    quiet = trader.btc_sigma()
    assert quiet and quiet > 0

    # an hour-scale floor larger than the tick noise must win
    trader.S.klines = [80000.0 + (i % 2) * 800.0 for i in range(30)]
    floored = trader.btc_sigma()
    assert floored is not None and floored > quiet


def test_fair_value_is_a_coin_flip_at_the_strike(S):
    trader.S.btc_hist.clear()
    for i in range(60):
        trader.S.btc_hist.append((1000.0 + i, 80000.0 + (i % 2) * 3.0))
    trader.S.klines = [80000.0 + (i % 2) * 3.0 for i in range(30)]
    trader.S.strike = 80000.0
    trader.S.btc = 80000.0
    trader.S.ws_start = 1  # any non-zero window so secs_left() is defined

    assert trader.fair_p() == pytest.approx(0.5, abs=1e-9)

    trader.S.btc = 80500.0
    assert trader.fair_p() > 0.5
    trader.S.btc = 79500.0
    assert trader.fair_p() < 0.5


def test_fair_value_needs_klines(S):
    trader.S.klines = []
    trader.S.strike = 80000.0
    trader.S.btc = 80000.0
    assert trader.fair_p() is None


def test_warnings_report_a_streak(S):
    trader.S.klines = [80000.0 + i for i in range(23)] + [80010.0 + i for i in range(7)]
    texts = [t for _, t in trader.warnings()]
    assert any("consecutive up bars" in t for t in texts)


def test_warnings_report_oversold(S):
    trader.S.klines = [90000.0 - i * 10 for i in range(40)]
    texts = [t for _, t in trader.warnings()]
    assert any("oversold" in t for t in texts)


def test_warnings_are_quiet_without_enough_bars(S):
    trader.S.klines = [80000.0] * 10
    assert trader.warnings() == []


def test_gate_checks_show_dashes_before_the_detector_warms(S):
    trader.S.metrics = {}
    trader.S.tokens = {}
    checks = trader.gate_checks()
    labels = {label: (ok, val) for label, ok, val in checks}
    assert labels["vol"] == (False, "\u2014")
    assert labels["px"] == (False, "\u2014")


def test_gate_checks_pass_on_their_own_rulebook(S):
    trader.S.metrics = {"travel": 0.50, "eff": 0.20, "drift": 0.01}
    trader.S.tokens = {"up": "TOK"}
    trader.S.books = {"TOK": trader.Book(asks={0.50: 10.0})}
    import time

    trader.S.ws_start = int(time.time()) - 60
    assert all(ok for _, ok, _ in trader.gate_checks())
