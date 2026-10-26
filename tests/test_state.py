"""Regressions for the terminal's state machine: rollover, bars, pause, live guards."""
from __future__ import annotations

import asyncio

import pytest

import trader


def test_rollover_settles_at_the_old_mark_not_at_zero(S):
    trader.S.tokens = {"up": "OLD", "dn": "OLDD"}
    trader.S.books = {"OLD": trader.Book(bids={0.55: 10.0})}
    trader.S.pos.side = "up"
    trader.S.pos.qty = 10.0
    trader.S.pos.entry = 0.50

    trader.roll_to_window(123, {"up": "NEW", "dn": "NEWD"})

    assert trader.S.pos.side == ""
    assert trader.S.trades[0]["exit"] == 0.55
    assert trader.S.trades[0]["pnl"] == pytest.approx(0.5)
    assert trader.S.tokens == {"up": "NEW", "dn": "NEWD"}
    assert "OLD" not in trader.S.books


def test_rollover_without_a_position_just_switches(S):
    trader.S.tokens = {"up": "OLD"}
    trader.S.books = {"OLD": trader.Book(bids={0.55: 1.0})}

    trader.roll_to_window(123, {"up": "NEW"})

    assert trader.S.tokens == {"up": "NEW"}
    assert trader.S.books == {}
    assert trader.S.trades == []


def test_new_minute_appends_a_closed_bar(S):
    trader.S.klines = [1.0, 2.0, 3.0]
    trader.S.bar_ts = 10
    trader.S.btc = 100.0

    trader.on_price_tick(101.0, now=11 * 60)

    assert trader.S.klines == [1.0, 2.0, 100.0, 101.0]
    assert trader.S.bar_ts == 11


def test_ticks_inside_the_same_minute_overwrite_the_forming_bar(S):
    trader.S.klines = [1.0, 2.0, 3.0]
    trader.S.bar_ts = 10

    trader.on_price_tick(7.0, now=10 * 60 + 5)
    trader.on_price_tick(9.0, now=10 * 60 + 40)

    assert trader.S.klines == [1.0, 2.0, 9.0]
    assert trader.S.btc == 9.0


def test_bar_series_is_trimmed_and_seeded_from_ticks(S):
    trader.S.klines = [float(i) for i in range(200)]
    trader.S.bar_ts = 10
    trader.on_price_tick(999.0, now=11 * 60)
    assert len(trader.S.klines) == 200

    trader.S.klines = []
    trader.S.bar_ts = 0
    trader.on_price_tick(42.0, now=12 * 60)
    assert trader.S.klines == [42.0]


def test_session_open_is_pinned_to_the_first_price(S):
    trader.S.klines = [1.0]
    trader.S.bar_ts = 10
    trader.on_price_tick(80000.0, now=10 * 60)
    trader.on_price_tick(80100.0, now=10 * 60 + 1)
    assert trader.S.session_open == 80000.0


def test_btc_history_keeps_a_full_two_minute_lookback(S):
    trader.S.btc_hist.clear()
    # 50 ticks/second for 120 seconds, drifting up 0.02 per tick
    for i in range(6000):
        trader.S.btc_hist.append((1000.0 + i * 0.02, 80000.0 + i * 0.02 + (i % 2) * 0.5))
    trader.S.btc = trader.S.btc_hist[-1][1]
    trader.S.strike = 80000.0

    st = trader.btc_state()
    assert st is not None
    # 60 seconds is 3000 ticks: 3000 * 0.02 = 60 dollars
    assert st["m60"] == pytest.approx(60.0, abs=1.0)


def test_pause_stops_the_detector_timer(S, monkeypatch):
    calls = []
    monkeypatch.setattr(trader, "recompute_detector", lambda: calls.append(1))

    class Dummy:
        is_mounted = True
        paint_books = staticmethod(lambda: None)
        check_exits = staticmethod(lambda: None)

    trader.S.paused = True
    trader.TraderApp.tick_view(Dummy())
    assert calls == []

    trader.S.paused = False
    trader.TraderApp.tick_view(Dummy())
    assert calls == [1]


def test_live_toggle_refuses_without_a_working_broker(S):
    class Broker:
        ok = False

    trader.S.live = False
    trader.S.broker = None
    trader.TraderApp.action_toggle_live(object.__new__(trader.TraderApp))
    assert trader.S.live is False
    assert "no live broker" in trader.S.msg

    trader.S.broker = Broker()
    trader.TraderApp.action_toggle_live(object.__new__(trader.TraderApp))
    assert trader.S.live is False


def test_live_toggle_works_with_a_ready_broker(S):
    class Broker:
        ok = True

    trader.S.live = False
    trader.S.broker = Broker()
    app = object.__new__(trader.TraderApp)
    trader.TraderApp.action_toggle_live(app)
    assert trader.S.live is True
    trader.TraderApp.action_toggle_live(app)
    assert trader.S.live is False


def test_failed_live_exits_do_not_retry_at_thirty_hertz(S, monkeypatch):
    sells = []

    async def fake_sell(self, mk):
        sells.append(mk)

    monkeypatch.setattr(trader.TraderApp, "_live_sell", fake_sell)

    class Broker:
        ok = True

    trader.S.live = True
    trader.S.broker = Broker()
    trader.S.busy = False
    trader.S.exit_retry_ts = 0.0

    async def go():
        app = object.__new__(trader.TraderApp)
        trader.TraderApp._close_at(app, "STOP -20%", 0.3)
        trader.S.busy = False          # pretend the attempt completed
        trader.TraderApp._close_at(app, "STOP -20%", 0.3)
        await asyncio.sleep(0)

    asyncio.run(go())

    assert sells == [0.3]


def test_stake_changes_are_persisted(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(trader, "CFG_PATH", str(path))
    trader.S.preset = "SCALP"

    app = object.__new__(trader.TraderApp)
    trader.TraderApp.action_stake(app, 7)
    assert trader.load_cfg() == {"preset": "SCALP", "stake": 7}

    trader.TraderApp.action_stake_cycle(app)
    assert trader.S.stake == 8
    assert trader.load_cfg()["stake"] == 8


def test_trade_log_is_capped(S):
    trader.S.tokens = {"up": "TOK"}
    for i in range(210):
        trader.S.books = {"TOK": trader.Book(bids={0.50: 1.0})}
        trader.S.pos.side = "up"
        trader.S.pos.qty = 1.0
        trader.S.pos.entry = 0.40
        trader.settle_position(f"t{i}")

    assert len(trader.S.trades) == 200
