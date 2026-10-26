"""AM/PM stamps and the persistent terminal-only trade history."""
from __future__ import annotations

import asyncio
import json
import re

from polytrader.engine import load_history, record_trade, settle_position
from polytrader.state import Book
from polytrader.ui import panels


def _open_position(S, entry=0.40, qty=10.0, exit_px=0.50):
    S.tokens = {"up": "TOK"}
    S.books = {"TOK": Book(bids={exit_px: 100.0})}
    S.pos.side = "up"
    S.pos.qty = qty
    S.pos.entry = entry


def test_settle_stamps_am_pm_time(S):
    _open_position(S)

    settle_position("test")

    assert re.fullmatch(r"\d{1,2}:\d{2}:\d{2} [AP]M", S.trades[0]["ts"])


def test_settle_appends_each_close_to_the_history_file(S, tmp_path, monkeypatch):
    from polytrader import config

    path = tmp_path / "hist.jsonl"
    monkeypatch.setattr(config, "HISTORY_PATH", str(path))
    _open_position(S)

    settle_position("sold")

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["reason"] == "sold"
    assert rows[0]["pnl"] == S.trades[0]["pnl"]


def test_load_history_skips_corrupt_lines_and_trims_the_file(tmp_path):
    path = tmp_path / "hist.jsonl"
    path.write_text('{"pnl": 1.0}\nnot json\n{"pnl": 2.0}\n[1,2]\n{"pnl": 3.0}\n')

    rows = load_history(limit=2, path=str(path))

    assert [r["pnl"] for r in rows] == [2.0, 3.0]
    assert len(path.read_text().splitlines()) == 2


def test_load_history_missing_file_is_empty(tmp_path):
    assert load_history(path=str(tmp_path / "nope.jsonl")) == []


def test_record_trade_never_raises(S, tmp_path):
    record_trade({"pnl": 1.0}, path=str(tmp_path / "missing" / "hist.jsonl"))


def test_session_log_header_shows_count_and_pnl(S):
    S.trades = [
        {"ts": "9:02:11 PM", "side": "up", "entry": 0.36, "exit": 0.50,
         "qty": 10.0, "pnl": 1.40, "pct": 0.389, "reason": "sold"},
        {"ts": "9:04:02 PM", "side": "dn", "entry": 0.30, "exit": 0.25,
         "qty": 10.0, "pnl": -0.50, "pct": -0.167, "reason": "stop"},
    ]
    S.pnl_session = 0.90

    text = panels.make_trades_text().plain

    assert "SESSION 2 trades" in text
    assert "P&L $+0.90" in text


def test_history_mode_merges_past_runs_with_combined_pnl(S):
    S.trades = [
        {"ts": "9:02:11 PM", "side": "up", "entry": 0.36, "exit": 0.50,
         "qty": 10.0, "pnl": 1.40, "pct": 0.389, "reason": "sold"},
    ]
    S.pnl_session = 1.40
    S.history = [
        {"ts": "8:01:00 PM", "side": "dn", "entry": 0.40, "exit": 0.60,
         "qty": 10.0, "pnl": 2.00, "pct": 0.50, "reason": "sold"},
    ]
    S.history_mode = True

    text = panels.make_trades_text().plain

    assert "ALL-TIME 2 trades" in text
    assert "P&L $+3.40" in text
    assert "8:01:00 PM" in text


def test_h_key_toggles_session_and_all_time(S, monkeypatch):
    from polytrader.ui import app as ui

    async def noop(*_a, **_k):
        return None

    for name in ("binance_loop", "window_loop", "chainlink_loop",
                 "rest_fallback_loop", "basis_loop", "clob_loop"):
        monkeypatch.setattr(ui, name, noop)
    monkeypatch.setattr(ui, "fetch_klines", lambda: [])
    application = ui.TraderApp()

    async def go():
        async with application.run_test() as pilot:
            assert S.history_mode is False
            await pilot.press("h")
            await pilot.pause()
            assert S.history_mode is True
            await pilot.press("J")
            await pilot.press("K")
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert S.history_mode is False

    asyncio.run(go())
