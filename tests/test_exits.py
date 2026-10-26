"""Exit state machine: the money rules, with no app and no network."""
from __future__ import annotations

import time

import pytest

from polytrader.engine import exit_decision, settle_position
from polytrader.state import Book, S, secs_left

ENTRY = 0.40


def test_take_profit_fires_at_the_target():
    reason, _ = exit_decision(ENTRY, ENTRY * 1.50, "SCALP")
    assert reason == "TAKE PROFIT +50%"


def test_take_profit_wins_even_with_breakeven_already_armed():
    reason, locked = exit_decision(ENTRY, ENTRY * 1.50, "SCALP", locked=True)
    assert reason == "TAKE PROFIT +50%"
    assert locked is True


def test_armed_breakeven_with_the_stop_off_does_nothing():
    reason, locked = exit_decision(
        ENTRY, ENTRY * 0.30, "SCALP", stop_on=False, locked=True)
    assert reason is None
    assert locked is True


def test_breakeven_arms_then_catches_a_round_trip():
    locked = False
    reason, locked = exit_decision(ENTRY, ENTRY * 1.31, "SCALP", locked=locked)
    assert reason is None and locked is True

    # price falls back to entry: the stop has moved up to breakeven
    reason, locked = exit_decision(ENTRY, ENTRY, "SCALP", locked=locked)
    assert reason == "BE STOP"


def test_breakeven_armed_but_still_above_entry_holds():
    reason, locked = exit_decision(ENTRY, ENTRY * 1.05, "SCALP", locked=True)
    assert reason is None and locked is True


def test_hard_stop_fires_when_breakeven_never_armed():
    reason, locked = exit_decision(ENTRY, ENTRY * 0.79, "SCALP")
    assert reason == "STOP -20%" and locked is False


def test_stop_off_still_honours_take_profit():
    reason, _ = exit_decision(ENTRY, ENTRY * 1.60, "SCALP", stop_on=False)
    assert reason == "TAKE PROFIT +50%"


def test_stop_off_lets_a_loser_run():
    reason, _ = exit_decision(ENTRY, ENTRY * 0.50, "SCALP", stop_on=False)
    assert reason is None


def test_free_has_no_stop_and_no_target():
    assert exit_decision(ENTRY, ENTRY * 0.10, "FREE")[0] is None
    assert exit_decision(ENTRY, ENTRY * 5.00, "FREE")[0] is None


def test_tight_profile_uses_its_own_levels():
    reason, _ = exit_decision(ENTRY, ENTRY * 1.30, "TIGHT")
    assert reason == "TAKE PROFIT +30%"
    assert exit_decision(ENTRY, ENTRY * 0.84, "TIGHT")[0] == "STOP -15%"


def test_unknown_preset_falls_back_to_scalp():
    reason, _ = exit_decision(ENTRY, ENTRY * 1.50, "NOPE")
    assert reason == "TAKE PROFIT +50%"


def test_settle_records_the_trade_and_clears_the_position(S):
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.50: 100.0})}
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40
    S.pos.locked = True

    settle_position("test")

    assert S.pos.side == ""
    assert S.pos.locked is False
    assert S.pnl_session == pytest.approx(1.0)
    assert S.trades[0]["pnl"] == pytest.approx(1.0)
    assert S.trades[0]["pct"] == pytest.approx(0.25)


def test_settle_can_use_an_explicit_fill_price(S):
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.45: 100.0})}
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40

    settle_position("sold", 0.50)

    assert S.trades[0]["exit"] == 0.50
    assert S.pnl_session == pytest.approx(1.0)


def test_a_partial_close_keeps_the_rest_open(S):
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.50: 100.0})}
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40

    settle_position("sold", 0.50, 4.0)

    assert S.pos.side == "up"
    assert S.pos.qty == 6.0
    assert S.trades[0]["qty"] == 4.0
    assert S.trades[0]["pnl"] == pytest.approx(0.4)

    settle_position("sold", 0.50, 6.0)

    assert S.pos.side == ""
    assert len(S.trades) == 2


def test_settle_tags_the_mode_and_rings_the_bell(S):
    from polytrader.engine import settle_position

    S.live = True
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40
    S.bell_pending = False
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.50: 100.0})}

    settle_position("sold")

    assert S.trades[0]["mode"] == "L"
    assert S.bell_pending is True
    assert S.flash_until > time.time()

    S.live = False
    S.pos.side = "up"
    S.pos.qty = 10.0
    S.pos.entry = 0.40

    settle_position("sold")

    assert S.trades[0]["mode"] == "P"


def test_dust_the_exchange_cannot_sell_is_absorbed(S):
    from polytrader.engine import settle_position

    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={0.50: 100.0})}
    S.pos.side = "up"
    S.pos.qty = 2.5
    S.pos.entry = 0.40

    settle_position("sold", 0.50, 2.496)

    assert S.pos.side == ""
    assert S.pos.qty == 0.0
    assert S.trades[0]["qty"] == pytest.approx(2.496)


def test_secs_left_counts_down_from_the_window_open(S):
    import time

    S.ws_start = int(time.time()) - 60
    left = secs_left()
    assert 239 <= left <= 240.5

    S.ws_start = 0
    assert secs_left() == 0.0
