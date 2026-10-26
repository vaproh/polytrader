"""The panels must show the resolution reference, not just a distance."""
from __future__ import annotations

import time

from polytrader.state import Book
from polytrader.ui import panels


def test_status_puts_the_beat_next_to_spot(S):
    S.btc = 79721.87
    S.strike = 79750.59
    S.twap = 79624.74
    S.twap_src = "chainlink"
    S.twap_ts = time.time()

    text = panels.make_status_text().plain

    assert "beat $79,750.59" in text
    assert "TWAP $79,624.74" in text
    assert "-125.85" in text
    assert "DOWN" in text
    assert "chainlink" in text


def test_window_shows_direction_and_both_distances(S):
    S.slug = "btc-updown-5m-1793025000"
    S.ws_start = int(time.time()) - 100
    S.btc = 79721.87
    S.strike = 79750.59
    S.twap = 79624.74
    S.twap_src = "chainlink"
    S.twap_ts = time.time()

    text = panels.make_window_text().plain

    assert "PRICE TO BEAT" in text
    assert "$79,750.59" in text
    assert "chainlink" in text
    assert "DOWN" in text
    assert "-125.85" in text
    assert "-28.72" in text
    assert "not resolution" in text


def test_window_flips_to_up_when_twap_is_above_the_beat(S):
    S.slug = "btc-updown-5m-1793025000"
    S.ws_start = int(time.time()) - 100
    S.btc = 79721.87
    S.strike = 79750.59
    S.twap = 79800.00
    S.twap_src = "chainlink"
    S.twap_ts = time.time()

    text = panels.make_window_text().plain

    assert "UP" in text
    assert "+49.41" in text


def test_window_labels_the_proxy_when_the_exact_feed_is_down(S):
    S.slug = "btc-updown-5m-1793025000"
    S.ws_start = int(time.time()) - 100
    S.strike = 79750.59
    S.twap = 79624.74
    S.twap_src = "proxy"

    text = panels.make_window_text().plain

    assert "(proxy)" in text


def test_books_show_both_pair_sums(S):
    S.tokens = {"up": "U", "dn": "D"}
    S.books = {
        "U": Book(bids={0.12: 2940}, asks={0.13: 410}),
        "D": Book(bids={0.87: 410}, asks={0.88: 2940}),
    }

    text = panels.make_books_text().plain

    assert "0.9900" in text
    assert "1.0100" in text
    assert "UP" in text and "DOWN" in text


def test_signal_does_not_print_a_fake_rsi_while_warming_up(S):
    S.regime = "CHOP"
    S.klines = [1.0] * 10

    text = panels.make_signal_text().plain

    assert "RSI(14) 1m -- (10/15 bars)" in text


def test_signal_prints_rsi_with_one_decimal(S):
    S.regime = "CHOP"
    S.klines = [float(i) for i in range(30)]

    text = panels.make_signal_text().plain

    assert "RSI(14) 1m 100.0" in text


def test_trades_show_the_close_reason(S):
    S.trades = [{
        "ts": "20:02:17", "side": "up", "entry": 0.36, "exit": 0.07,
        "qty": 13.9, "pnl": -4.03, "pct": -0.806, "reason": "sold",
    }]

    text = panels.make_trades_text().plain

    assert "sold" in text


def test_signal_labels_base_rates_as_the_token_bid(S):
    S.regime = "CHOP-DOWN"
    S.rates = {"r20": 0.308, "r50": 0.192, "n": 130178,
               "med": -0.154, "mean": 0.124}

    text = panels.make_signal_text().plain

    assert "token bid 60s" in text
    assert "+20% 30.8%" in text


def test_free_preset_does_not_claim_the_stop_is_armed(S):
    S.pos.side = "up"
    S.pos.qty = 17.2
    S.pos.entry = 0.29
    S.pos.ts = time.time()
    S.preset = "FREE"
    S.stop_on = True
    S.tokens = {"up": "U"}
    S.books = {"U": Book(bids={0.30: 1})}

    text = panels.make_position_text().plain

    assert "stop none" in text
    assert "stop ARMED" not in text


def test_alerts_label_cumulative_cvd_as_session(S):
    S.cvd = 146.6
    S.trade_hist.append((time.time(), 1.0, True))

    text = panels.make_alerts_text().plain

    assert "CVD session +146.6" in text
