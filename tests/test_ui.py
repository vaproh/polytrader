"""The panels must show the resolution reference, not just a distance."""
from __future__ import annotations

import asyncio
import time

import pytest
from textual.app import App
from textual.widgets import Input

from polytrader.state import Book
from polytrader.ui import panels
from polytrader.ui.app import AmountScreen, parse_amount


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

    assert "99.0¢" in text
    assert "101.0¢" in text
    assert "12.0¢" in text
    assert "88.0¢" in text
    assert "UP" in text and "DOWN" in text


def test_signal_warms_each_rsi_period_in_turn(S):
    S.regime = "CHOP"
    S.klines = [1.0] * 10

    text = panels.make_signal_text().plain

    assert "6:" in text
    assert "12:" not in text
    assert "14:" not in text
    assert "24:" not in text


def test_signal_shows_nothing_fake_before_the_fastest_rsi(S):
    S.regime = "CHOP"
    S.klines = [1.0] * 5

    text = panels.make_signal_text().plain

    assert "-- (5/7 bars)" in text


def test_signal_prints_every_rsi_with_one_decimal(S):
    S.regime = "CHOP"
    S.klines = [float(i) for i in range(30)]

    text = panels.make_signal_text().plain

    assert "6:100.0" in text
    assert "12:100.0" in text
    assert "14:100.0" in text
    assert "24:100.0" in text


def test_signal_shows_per_second_rsi(S):
    S.btc_hist.clear()
    S.btc_hist.extend((2000.0 + i, 80000.0 + i * 10) for i in range(20))

    assert "1s:--" not in panels.make_signal_text().plain

    S.btc_hist.clear()

    assert "1s:--" in panels.make_signal_text().plain


def test_trades_show_the_close_reason(S):
    S.trades = [{
        "ts": "20:02:17", "side": "up", "entry": 0.36, "exit": 0.07,
        "qty": 13.9, "pnl": -4.03, "pct": -0.806, "reason": "sold",
    }]

    text = panels.make_trades_text().plain

    assert "36.0¢ -> 7.0¢" in text
    assert "sold" in text


def test_window_shows_candle_moves_with_dollar_amounts(S):
    import time

    S.strike = 80000.0
    S.twap = 80130.58
    S.twap_src = "chainlink"
    S.twap_open = 80125.00
    S.twap_open_bar = int(time.time()) // 60
    S.btc = 80327.93
    S.candle_open = 80330.00

    text = panels.make_window_text().plain

    assert "▲ +5.58" in text
    assert "▼ -2.07" in text


def test_window_hides_arrows_before_the_first_tick(S):
    S.strike = 80000.0
    S.twap = 80130.58
    S.twap_open = 0.0
    S.twap_open_bar = 0
    S.btc = 80327.93
    S.candle_open = 0.0

    text = panels.make_window_text().plain

    assert "▲" not in text and "▼" not in text


def test_window_hides_the_twap_arrow_on_the_proxy_feed(S):
    import time

    S.strike = 80000.0
    S.twap = 80130.58
    S.twap_src = "proxy"
    S.twap_open = 80125.00
    S.twap_open_bar = int(time.time()) // 60
    S.btc = 80327.93
    S.candle_open = 80330.00

    text = panels.make_window_text().plain

    assert "▲" not in text
    assert "▼ -2.07" in text  # the spot arrow still works


def test_next_key_needs_the_last_minute(S, monkeypatch):
    from polytrader.ui.app import TraderApp

    app = object.__new__(TraderApp)
    W = 1793035800
    monkeypatch.setattr(time, "time", lambda: W + 100.0)  # 200s left
    S.ws_start = W

    TraderApp.action_next_window(app)

    assert S.ws_start == W
    assert "60s" in S.msg


def test_next_key_needs_flat(S, monkeypatch):
    from polytrader.ui.app import TraderApp

    app = object.__new__(TraderApp)
    W = 1793035800
    monkeypatch.setattr(time, "time", lambda: W + 250.0)  # 50s left
    S.ws_start = W
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.50

    TraderApp.action_next_window(app)

    assert S.ws_start == W
    assert "flat only" in S.msg


def test_next_key_switches_when_the_book_is_live(S, monkeypatch):
    from polytrader.ui.app import TraderApp

    app = object.__new__(TraderApp)
    W = 1793035800
    monkeypatch.setattr(time, "time", lambda: W + 250.0)  # 50s left
    S.ws_start = W
    S.tokens = {"up": "OLD", "dn": "OLDD"}
    S.next_tokens = {"up": "NU", "dn": "ND"}
    S.next_ws = W + 300
    S.next_books = {"NU": Book(bids={0.12: 10.0}, asks={0.13: 5.0}),
                    "ND": Book(bids={0.87: 10.0}, asks={0.88: 5.0})}

    TraderApp.action_next_window(app)

    assert S.ws_start == W + 300
    assert S.tokens == {"up": "NU", "dn": "ND"}


def test_new_minute_opens_a_fresh_candle(S):
    from polytrader.feeds.binance import on_price_tick

    S.klines = [80000.0]
    S.bar_ts = 100
    S.btc = 80000.0
    S.twap = 79950.0
    S.twap_open = 79940.0
    S.twap_open_bar = 99

    on_price_tick(80100.0, now=101 * 60)

    assert S.candle_open == 80100.0
    assert S.twap_open == 79940.0  # ticks never touch the TWAP open


def test_window_stacks_the_windows_own_candles(S):
    W = 1793035800
    S.ws_start = W
    S.klines = [79960.0, 79970.0, 79980.0, 80000.0, 79997.0, 80010.0]
    S.bar_ts = (W + 160) // 60  # third window minute forming

    text = panels.make_window_text().plain

    assert "(1) ▲+20.00" in text
    assert "(2) ▼-3.00" in text
    assert "(3) ▲+13.00" in text
    assert "(4)" not in text


def test_candle_stack_stops_where_history_runs_out(S):
    S.ws_start = 1793035800
    S.klines = [80000.0]
    S.bar_ts = 1793035800 // 60

    assert panels.window_candles() == []
    assert "CANDLES" not in panels.make_window_text().plain


def test_twap_open_latches_the_first_exact_print_of_the_minute(S):
    from polytrader.state import latch_twap_open

    minute = 1793035800 // 60 * 60
    S.twap = 80130.0
    S.twap_src = "chainlink"

    latch_twap_open(now=minute + 5)

    assert S.twap_open == 80130.0
    assert S.twap_open_bar == minute // 60

    S.twap = 80120.0  # later prints do not move the open
    latch_twap_open(now=minute + 50)
    assert S.twap_open == 80130.0

    S.twap_src = "proxy"  # the proxy never latches
    S.twap = 80100.0
    latch_twap_open(now=minute + 300)
    assert S.twap_open == 80130.0


def test_refresh_next_stores_tokens_and_books(S, monkeypatch):
    import asyncio

    from polytrader import poly as polymod
    from polytrader.feeds import window as win

    monkeypatch.setattr(polymod, "market", lambda slug: {
        "outcomes": ["Up", "Down"], "token_ids": ["U", "D"]})
    monkeypatch.setattr(polymod, "book", lambda aid: {
        "best_bid": 0.40, "bid_size": 10.0,
        "best_ask": 0.42, "ask_size": 5.0})
    S.ws_start = 1793035800

    asyncio.run(win._refresh_next(1793036100))

    assert S.next_tokens == {"up": "U", "dn": "D"}
    assert S.next_books["U"].best_ask() == pytest.approx(0.42)
    assert S.next_slug == "btc-updown-5m-1793036100"


def test_books_preview_the_next_window(S):
    import time

    S.ws_start = int(time.time())
    S.next_tokens = {"up": "NU", "dn": "ND"}
    S.next_books = {"NU": Book(bids={0.12: 10.0}, asks={0.13: 5.0}),
                    "ND": Book(bids={0.87: 10.0}, asks={0.88: 5.0})}

    text = panels.make_books_text().plain

    assert "NEXT btc-updown-5m-" in text
    assert "starts in" in text
    assert "12.0¢/13.0¢" in text
    assert "87.0¢/88.0¢" in text

    S.next_tokens = {}
    S.next_books = {}
    S.next_slug = ""

    assert "not listed yet" in panels.make_books_text().plain


def test_window_counts_down_to_the_open_after_an_early_switch(S):
    import time

    S.ws_start = int(time.time()) + 45
    S.slug = "btc-updown-5m-1793036100"

    text = panels.make_window_text().plain

    assert "opens in 0:" in text
    assert "closes in" not in text


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


def test_parse_amount_accepts_dollars_and_commas():
    assert parse_amount("$1,234.5") == 1234.5
    assert parse_amount(" 12.346 ") == 12.35


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "1e9"])
def test_parse_amount_rejects_bad_amounts(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_status_shows_cash_order_and_after(S):
    S.live = True
    S.balance = 100.0
    S.stake = 12.34
    S.funder = "0x1234567890"

    text = panels.make_status_text().plain

    assert "CASH $100.00" in text
    assert "ORDER $12.34" in text
    assert "AFTER $87.66" in text


def test_status_in_paper_does_not_claim_a_cash_balance(S):
    S.live = False

    text = panels.make_status_text().plain

    assert "CASH -- (paper)" in text


def test_buy_refuses_when_no_amount_is_set(S):
    from polytrader.ui.app import TraderApp

    S.tokens = {"up": "U"}
    S.books = {"U": Book(bids={0.28: 1}, asks={0.31: 1})}
    S.stake = 0.0

    app = object.__new__(TraderApp)
    TraderApp.action_buy(app, "up")

    assert S.msg == "no amount set -- press a first"
    assert not S.pos.side


def test_terminal_starts_disarmed(S):
    assert S.stake == 0.0

    status = panels.make_status_text().plain

    assert "ORDER -- (press a)" in status

    S.tokens = {"up": "U", "dn": "D"}
    S.books = {
        "U": Book(bids={0.49: 1}, asks={0.50: 1}),
        "D": Book(bids={0.49: 1}, asks={0.50: 1}),
    }
    books = panels.make_books_text().plain

    assert "ORDER  -- (press a)" in books


def test_books_show_the_shares_the_stake_buys(S):
    S.stake = 10.0
    S.tokens = {"up": "U", "dn": "D"}
    S.books = {
        "U": Book(bids={0.49: 1}, asks={0.50: 1}),
        "D": Book(bids={0.49: 1}, asks={0.50: 1}),
    }

    text = panels.make_books_text().plain

    assert "$10.00 -> UP 20.0sh" in text
    assert "DOWN 20.0sh" in text


def test_position_shows_cost_and_value(S):
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.29
    S.pos.ts = time.time()
    S.tokens = {"up": "U"}
    S.books = {"U": Book(bids={0.30: 1})}

    text = panels.make_position_text().plain

    assert "cost $2.90" in text
    assert "value $3.00" in text
    assert "pays $10.00" in text
    assert "29.0¢" in text


def test_position_shows_dollar_stop_and_tp_prices(S):
    S.preset = "SCALP"
    S.risk = {"SCALP": {"stop": {"by": "usd", "v": 0.40},
                        "tp": {"by": "usd", "v": 0.50}}}
    S.pos.side = "up"
    S.pos.qty = 4.0
    S.pos.entry = 0.50
    S.pos.ts = time.time()
    S.tokens = {"up": "U"}
    S.books = {"U": Book(bids={0.55: 1})}

    text = panels.make_position_text().plain

    assert "40.0¢" in text
    assert "62.5¢" in text
    assert "-$0.40" in text and "+$0.50" in text


def test_window_shows_a_progress_bar(S):
    S.slug = "btc-updown-5m-1793025000"
    S.ws_start = int(time.time()) - 150

    text = panels.make_window_text().plain

    assert "50%" in text


def test_status_flashes_a_fresh_close(S):
    S.flash_until = time.time() + 2.0

    assert "FILLED" in panels.make_status_text().plain

    S.flash_until = time.time() - 1.0

    assert "FILLED" not in panels.make_status_text().plain


def test_books_show_fresh_or_stale(S):
    import time as _time

    S.tokens = {"up": "U", "dn": "D"}
    S.books = {
        "U": Book(bids={0.12: 2940}, asks={0.13: 410}),
        "D": Book(bids={0.87: 410}, asks={0.88: 2940}),
    }
    S.book_ts = _time.time()

    assert "fresh" in panels.make_books_text().plain

    S.book_ts = _time.time() - 30

    assert "stale" in panels.make_books_text().plain


def test_trades_show_the_mode_letter(S):
    S.trades = [{
        "ts": "20:02:17", "side": "up", "entry": 0.36, "exit": 0.07,
        "qty": 13.9, "pnl": -4.03, "pct": -0.806, "reason": "sold",
        "mode": "L",
    }]

    text = panels.make_trades_text().plain

    assert " L " in text


def test_parse_risk_value_accepts_percent_and_dollars():
    from polytrader.ui.app import parse_risk_value

    assert parse_risk_value("25") == ("pct", 0.25)
    assert parse_risk_value("25%") == ("pct", 0.25)
    assert parse_risk_value("$0.50") == ("usd", 0.50)
    assert parse_risk_value("50c") == ("usd", 0.50)
    assert parse_risk_value("50¢") == ("usd", 0.50)
    assert parse_risk_value("$1,000") == ("usd", 1000.0)
    assert parse_risk_value("0") == ("pct", 0.0)


def test_parse_risk_value_rejects_garbage():
    import pytest

    from polytrader.ui.app import parse_risk_value

    for raw in ["", "abc", "-5", "1001%", "$-1"]:
        with pytest.raises(ValueError):
            parse_risk_value(raw)


def test_bell_toggle_flips_the_flag(S):
    from polytrader.ui.app import TraderApp

    app = object.__new__(TraderApp)
    S.bell = True

    TraderApp.action_toggle_bell(app)
    assert S.bell is False

    TraderApp.action_toggle_bell(app)
    assert S.bell is True


def test_exits_toggle_disables_tp_and_stop_together(S):
    from polytrader.ui.app import TraderApp

    app = object.__new__(TraderApp)

    TraderApp.action_toggle_exits(app)
    assert S.stop_on is False
    assert S.tp_on is False

    TraderApp.action_toggle_exits(app)
    assert S.stop_on is True
    assert S.tp_on is True


def test_position_panel_shows_exits_off(S):
    from polytrader.state import Book

    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.45: 100.0})}
    S.stop_on = False
    S.tp_on = False

    text = panels.make_position_text().plain

    assert "tp OFF" in text


def test_risk_modal_applies_per_preset_numbers_for_the_session_only(S, tmp_path, monkeypatch):
    import json

    from polytrader import config
    from polytrader.ui import app as ui

    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))
    S.preset = "FREE"
    S.risk = {}

    async def noop(*_a, **_k):
        return None

    for name in ("binance_loop", "window_loop", "chainlink_loop",
                 "rest_fallback_loop", "basis_loop", "clob_loop"):
        monkeypatch.setattr(ui, name, noop)
    monkeypatch.setattr(ui, "fetch_klines", lambda: [])

    app = ui.TraderApp()

    async def go():
        async with app.run_test() as pilot:
            await pilot.press("e")
            await pilot.pause()
            stop = app.screen.query_one("#risk-stop", Input)
            tp = app.screen.query_one("#risk-tp", Input)
            stop.value = "$0.40"
            tp.value = "50%"
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(go())

    assert S.risk == {"FREE": {
        "stop": {"by": "usd", "v": 0.40},
        "tp": {"by": "pct", "v": 0.50}}}
    assert "risk" not in json.loads(path.read_text())


class _ModalHost(App):
    def __init__(self):
        super().__init__()
        self.result = "not set"

    def on_mount(self):
        self.push_screen(AmountScreen(5.0), self._got)

    def _got(self, value):
        self.result = value


def test_amount_screen_returns_a_typed_dollar_amount(S):
    app = _ModalHost()

    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app.screen.query_one("#amount-input", Input)
            inp.value = "$12.34"
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(go())

    assert app.result == 12.34


def test_amount_screen_warns_about_the_exchange_minimum(S):
    from textual.widgets import Label

    app = _ModalHost()

    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            hint = app.screen.query_one("#amount-hint", Label)
            text = hint.render().plain
            assert "$1" in text

    asyncio.run(go())


def test_amount_binding_sets_the_stake(S, monkeypatch):
    from polytrader.ui import app as ui

    async def noop(*_a, **_k):
        return None

    for name in ("binance_loop", "window_loop", "chainlink_loop",
                 "rest_fallback_loop", "basis_loop", "clob_loop"):
        monkeypatch.setattr(ui, name, noop)
    monkeypatch.setattr(ui, "fetch_klines", lambda: [])

    app = ui.TraderApp()

    async def go():
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            inp = app.screen.query_one("#amount-input", Input)
            inp.value = "12.34"
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(go())

    assert S.stake == 12.34


def test_live_sell_books_dust_instead_of_sending_it(S, monkeypatch):
    from polytrader.state import Book
    from polytrader.ui import app as ui

    async def noop(*_a, **_k):
        return None

    for name in ("binance_loop", "window_loop", "chainlink_loop",
                 "rest_fallback_loop", "basis_loop", "clob_loop"):
        monkeypatch.setattr(ui, name, noop)
    monkeypatch.setattr(ui, "fetch_klines", lambda: [])
    S.tokens = {"dn": "TOK"}
    S.books = {"TOK": Book(bids={0.05: 100.0})}
    S.pos.side = "dn"
    S.pos.qty = 0.007
    S.pos.entry = 0.35
    S.stop_on = False  # the mounted detector must not paper-stop the position

    app = ui.TraderApp()

    async def go():
        async with app.run_test() as pilot:
            await app._live_sell(0.05)
            await pilot.pause()

    asyncio.run(go())

    assert S.pos.side == ""
    assert S.trades[0]["reason"] == "dust"
