"""Detector, volatility, fair value and warnings: the numbers the panels show."""
from __future__ import annotations

import pytest

from polytrader import detector
from polytrader.detector import BASE_RATES
from polytrader.state import Book, S


def feed(regime_mids):
    """Fill the 250ms mid series so the detector sees a full 36s span."""
    S.mid_hist.clear()
    for i, m in enumerate(regime_mids):
        S.mid_hist.append((i, m))


def trend(n=160, start=0.50, step=0.001):
    return [start + i * step for i in range(n)]


def test_fewer_than_four_buckets_is_warmup():
    feed([0.5] * 3)
    detector.recompute_detector()
    assert S.regime == "WARMUP"


def test_a_short_span_is_still_warmup():
    feed([0.5 + i * 0.001 for i in range(40)])  # 10s of history
    detector.recompute_detector()
    assert S.regime == "WARMUP"
    assert S.rates == {}


def test_flat_tape_is_dead():
    feed([0.50] * 160)
    detector.recompute_detector()
    assert S.regime == "DEAD"
    assert S.rates == BASE_RATES["DEAD"]


def test_straight_line_is_trend():
    feed(trend())
    detector.recompute_detector()
    assert S.regime == "TREND"
    assert S.metrics["eff"] == pytest.approx(1.0, abs=1e-6)
    assert S.metrics["revs"] == 0
    assert S.metrics["travel"] == pytest.approx(0.144, abs=1e-6)


def test_choppy_up_is_chop():
    # wide oscillation with a tiny positive net: low efficiency, net > 0
    mids = [0.50 + (0.01 if i % 2 else 0.0) + i * 0.0001 for i in range(160)]
    feed(mids)
    detector.recompute_detector()
    assert S.regime == "CHOP"
    assert S.metrics["net"] > 0
    assert S.metrics["eff"] < 0.35


def test_choppy_down_is_chop_down():
    mids = [0.60 - (0.01 if i % 2 else 0.0) - i * 0.0001 for i in range(160)]
    feed(mids)
    detector.recompute_detector()
    assert S.regime == "CHOP-DOWN"
    assert S.metrics["net"] < 0


def test_violent_tape_is_volatile():
    # up 0.006 / down 0.002: travel 0.64, net 0.32, eff 0.5
    mids, p = [], 0.50
    for i in range(160):
        p += 0.006 if i % 2 else -0.002
        mids.append(p)
    feed(mids)
    detector.recompute_detector()
    assert S.regime == "VOLATILE"
    assert S.metrics["travel"] >= 0.40
    assert 0.35 <= S.metrics["eff"] < 0.70


def test_drifting_quiet_tape_is_quiet_up():
    mids, p = [], 0.50
    for i in range(160):
        p += 0.003 if i % 2 else -0.001
        mids.append(p)
    feed(mids)
    detector.recompute_detector()
    assert S.regime == "QUIET-UP"
    assert S.metrics["travel"] < 0.40
    assert S.metrics["net"] > 0


def test_drift_is_net_over_the_first_kept_bucket():
    feed(trend())
    detector.recompute_detector()
    m = S.metrics
    # the 36s window starts at bucket 15, so mid[0] is 0.515
    assert m["drift"] == pytest.approx(m["net"] / 0.515, abs=1e-9)


def test_rsi_is_100_on_a_monotonic_rise_and_50_while_short():
    assert detector.rsi_last([1, 2, 3]) == 50.0
    assert detector.rsi_last([float(i) for i in range(30)]) == pytest.approx(100.0)


def test_btc_sigma_ignores_a_dead_second_and_uses_the_floor(S):
    # 60 seconds of ticks with a real range
    S.btc_hist.clear()
    for i in range(60):
        S.btc_hist.append((1000.0 + i, 80000.0 + (i % 2) * 3.0))
    quiet = detector.btc_sigma()
    assert quiet and quiet > 0

    # an hour-scale floor larger than the tick noise must win
    S.klines = [80000.0 + (i % 2) * 800.0 for i in range(30)]
    floored = detector.btc_sigma()
    assert floored is not None and floored > quiet


def test_fair_value_is_a_coin_flip_at_the_strike(S):
    S.btc_hist.clear()
    for i in range(60):
        S.btc_hist.append((1000.0 + i, 80000.0 + (i % 2) * 3.0))
    S.klines = [80000.0 + (i % 2) * 3.0 for i in range(30)]
    S.strike = 80000.0
    S.btc = 80000.0
    S.ws_start = 1  # any non-zero window so secs_left() is defined

    assert detector.fair_p() == pytest.approx(0.5, abs=1e-9)

    S.btc = 80500.0
    assert detector.fair_p() > 0.5
    S.btc = 79500.0
    assert detector.fair_p() < 0.5


def test_fair_value_needs_klines(S):
    S.klines = []
    S.strike = 80000.0
    S.btc = 80000.0
    assert detector.fair_p() is None


def test_warnings_report_a_streak(S):
    S.klines = [80000.0 + i for i in range(23)] + [80010.0 + i for i in range(7)]
    texts = [t for _, t in detector.warnings()]
    assert any("consecutive up bars" in t for t in texts)


def test_warnings_report_oversold(S):
    S.klines = [90000.0 - i * 10 for i in range(40)]
    texts = [t for _, t in detector.warnings()]
    assert any("oversold" in t for t in texts)


def test_warnings_are_quiet_without_enough_bars(S):
    S.klines = [80000.0] * 10
    assert detector.warnings() == []


def test_gate_checks_show_dashes_before_the_detector_warms(S):
    S.metrics = {}
    S.tokens = {}
    checks = detector.gate_checks()
    labels = {label: (ok, val) for label, ok, val in checks}
    assert labels["vol"] == (False, "\u2014")
    assert labels["px"] == (False, "\u2014")


def test_gate_checks_pass_on_their_own_rulebook(S):
    S.metrics = {"travel": 0.50, "eff": 0.20, "drift": 0.01}
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(asks={0.50: 10.0})}
    import time

    S.ws_start = int(time.time()) - 60
    assert all(ok for _, ok, _ in detector.gate_checks())
