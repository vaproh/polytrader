"""Regressions for the terminal's state machine: rollover, bars, pause, live guards."""
from __future__ import annotations

import asyncio

import pytest

from polytrader import config
from polytrader.detector import btc_state
from polytrader.engine import roll_to_window, settle_position
from polytrader.feeds.binance import on_price_tick
from polytrader.state import Book, S
from polytrader.ui import app as app_mod
from polytrader.ui.app import TraderApp


def test_rollover_settles_at_the_old_mark_not_at_zero(S):
    S.tokens = {"up": "OLD", "dn": "OLDD"}
    S.books = {"OLD": Book(bids={0.55: 10.0})}
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.50

    roll_to_window(123, {"up": "NEW", "dn": "NEWD"})

    assert S.pos.side == ""
    assert S.trades[0]["exit"] == 0.55
    assert S.trades[0]["pnl"] == pytest.approx(0.5)
    assert S.trades[0]["reason"] == "window rolled"
    assert S.tokens == {"up": "NEW", "dn": "NEWD"}
    assert "OLD" not in S.books


def test_rollover_without_a_position_just_switches(S):
    S.tokens = {"up": "OLD"}
    S.books = {"OLD": Book(bids={0.55: 1.0})}

    roll_to_window(123, {"up": "NEW"})

    assert S.tokens == {"up": "NEW"}
    assert S.books == {}
    assert S.trades == []


def test_new_minute_appends_a_closed_bar(S):
    S.klines = [1.0, 2.0, 3.0]
    S.bar_ts = 10
    S.btc = 100.0

    on_price_tick(101.0, now=11 * 60)

    assert S.klines == [1.0, 2.0, 100.0, 101.0]
    assert S.bar_ts == 11


def test_ticks_inside_the_same_minute_overwrite_the_forming_bar(S):
    S.klines = [1.0, 2.0, 3.0]
    S.bar_ts = 10

    on_price_tick(7.0, now=10 * 60 + 5)
    on_price_tick(9.0, now=10 * 60 + 40)

    assert S.klines == [1.0, 2.0, 9.0]
    assert S.btc == 9.0


def test_bar_series_is_trimmed_and_seeded_from_ticks(S):
    S.klines = [float(i) for i in range(200)]
    S.bar_ts = 10
    on_price_tick(999.0, now=11 * 60)
    assert len(S.klines) == 200

    S.klines = []
    S.bar_ts = 0
    on_price_tick(42.0, now=12 * 60)
    assert S.klines == [42.0]


def test_session_open_is_pinned_to_the_first_price(S):
    S.klines = [1.0]
    S.bar_ts = 10
    on_price_tick(80000.0, now=10 * 60)
    on_price_tick(80100.0, now=10 * 60 + 1)
    assert S.session_open == 80000.0


def test_btc_history_keeps_a_full_two_minute_lookback(S):
    S.btc_hist.clear()
    # 50 ticks/second for 120 seconds, drifting up 0.02 per tick
    for i in range(6000):
        S.btc_hist.append((1000.0 + i * 0.02, 80000.0 + i * 0.02 + (i % 2) * 0.5))
    S.btc = S.btc_hist[-1][1]
    S.strike = 80000.0

    st = btc_state()
    assert st is not None
    # 60 seconds is 3000 ticks: 3000 * 0.02 = 60 dollars
    assert st["m60"] == pytest.approx(60.0, abs=1.0)


def test_pause_stops_the_detector_timer(S, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "recompute_detector", lambda: calls.append(1))

    class Dummy:
        is_mounted = True
        paint_books = staticmethod(lambda: None)
        check_exits = staticmethod(lambda: None)

    S.paused = True
    TraderApp.tick_view(Dummy())
    assert calls == []

    S.paused = False
    TraderApp.tick_view(Dummy())
    assert calls == [1]


def test_live_toggle_refuses_without_a_working_broker(S):
    class Broker:
        ok = False

    S.live = False
    S.broker = None
    TraderApp.action_toggle_live(object.__new__(TraderApp))
    assert S.live is False
    assert "no live broker" in S.msg

    S.broker = Broker()
    TraderApp.action_toggle_live(object.__new__(TraderApp))
    assert S.live is False


def test_live_toggle_works_with_a_ready_broker(S):
    class Broker:
        ok = True

    S.live = False
    S.broker = Broker()
    app = object.__new__(TraderApp)
    TraderApp.action_toggle_live(app)
    assert S.live is True
    TraderApp.action_toggle_live(app)
    assert S.live is False


def test_failed_live_exits_do_not_retry_at_thirty_hertz(S, monkeypatch):
    sells = []

    async def fake_sell(self, mk):
        sells.append(mk)

    monkeypatch.setattr(TraderApp, "_live_sell", fake_sell)

    class Broker:
        ok = True

    S.live = True
    S.broker = Broker()
    S.busy = False
    S.exit_retry_ts = 0.0

    async def go():
        app = object.__new__(TraderApp)
        TraderApp._close_at(app, "STOP -20%", 0.3)
        S.busy = False          # pretend the attempt completed
        TraderApp._close_at(app, "STOP -20%", 0.3)
        await asyncio.sleep(0)

    asyncio.run(go())

    assert sells == [0.3]


def test_stake_changes_are_persisted(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))
    S.preset = "SCALP"

    app = object.__new__(TraderApp)
    TraderApp.action_stake(app, 7)
    assert config.load_cfg() == {"preset": "SCALP", "stake": 7, "risk": {}}

    TraderApp.action_stake_cycle(app)
    assert S.stake == 8
    assert config.load_cfg()["stake"] == 8


def test_custom_stake_keeps_cents(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))
    S.preset = "SCALP"

    app = object.__new__(TraderApp)
    TraderApp.action_stake(app, 12.5)
    assert S.stake == 12.5
    assert config.load_cfg()["stake"] == 12.5

    TraderApp.action_stake_cycle(app)
    assert S.stake == 13.5
    assert config.load_cfg()["stake"] == 13.5


def test_trade_log_is_capped(S):
    S.tokens = {"up": "TOK"}
    for i in range(210):
        S.books = {"TOK": Book(bids={0.50: 1.0})}
        S.pos.side = "up"
        S.pos.qty = 1.0
        S.pos.entry = 0.40
        settle_position(f"t{i}")

    assert len(S.trades) == 200


def test_kline_seed_retries_after_a_failed_rest_call(S, monkeypatch):
    calls = []

    def fake_fetch():
        calls.append(1)
        return [] if len(calls) == 1 else [1.0] * 30

    async def no_sleep(_):
        return None

    S.klines = []
    monkeypatch.setattr(app_mod, "fetch_klines", fake_fetch)
    monkeypatch.setattr(app_mod.asyncio, "sleep", no_sleep)

    asyncio.run(TraderApp._seed_klines(object.__new__(TraderApp)))

    assert len(calls) == 2
    assert len(S.klines) == 30


def test_failed_seed_does_not_wipe_tick_bars(S, monkeypatch):
    S.klines = [1.0] * 40
    monkeypatch.setattr(app_mod, "fetch_klines", lambda: [])

    asyncio.run(TraderApp._seed_klines(object.__new__(TraderApp)))

    assert S.klines == [1.0] * 40
