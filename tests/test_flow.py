"""Live taker tape: parsing, dedupe, and the alerts-panel FLOW line."""
from __future__ import annotations

import asyncio

from polytrader import poly as polymod
from polytrader.ui import panels
from polytrader.feeds import flow as flowmod


def test_recent_trades_parses_dollar_amounts(monkeypatch):
    monkeypatch.setattr(polymod, "_get_retry", lambda *a, **k: [
        {"timestamp": 100.0, "outcome": "Up", "size": 10.0, "price": 0.5},
        {"timestamp": 102.0, "outcome": "Down", "size": 300.0, "price": 0.99},
        {"timestamp": "junk"},
    ])

    rows = polymod.recent_trades("0xcond")

    assert [(r["side"], r["usd"]) for r in rows] == [
        ("dn", 297.0), ("up", 5.0)]


def test_recent_trades_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(polymod, "_get_retry", boom)

    assert polymod.recent_trades("0xcond") == []


def test_poll_merges_without_duplicates(S, monkeypatch):
    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.cond = "0xcond"
    feed = [
        {"ts": 102.0, "side": "dn", "usd": 297.0, "px": 0.99},
        {"ts": 100.0, "side": "up", "usd": 5.0, "px": 0.5},
    ]
    monkeypatch.setattr(polymod, "recent_trades", lambda cond: list(feed))

    asyncio.run(flowmod._poll())
    asyncio.run(flowmod._poll())

    assert [(f["side"], f["usd"]) for f in S.flow] == [
        ("dn", 297.0), ("up", 5.0)]


def test_poll_starts_a_fresh_tape_for_the_next_window(S, monkeypatch):
    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.cond = "0xcond"
    S.flow = [{"ts": 1.0, "side": "up", "usd": 5.0, "px": 0.5}]
    monkeypatch.setattr(polymod, "recent_trades", lambda cond: [])

    S.ws_start = 1793036100
    asyncio.run(flowmod._poll())

    assert S.flow == []


def test_poll_fetches_the_event_id_on_a_fresh_start(S, monkeypatch):
    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.cond = ""
    S.flow = []
    monkeypatch.setattr(polymod, "market",
                        lambda slug: {"condition_id": "0xcond"})
    monkeypatch.setattr(polymod, "recent_trades", lambda cond: [
        {"ts": 100.0, "side": "up", "usd": 5.0, "px": 0.5},
    ])

    asyncio.run(flowmod._poll())

    assert S.cond == "0xcond"
    assert [f["usd"] for f in S.flow] == [5.0]


def test_alerts_show_the_flow_tape(S):
    S.flow = [
        {"ts": 102.0, "side": "dn", "usd": 297.0, "px": 0.99},
        {"ts": 100.0, "side": "up", "usd": 24.0, "px": 0.5},
    ]

    text = panels.make_alerts_text().plain

    assert "FLOW" in text
    assert "+$297" in text
    assert "+$24" in text


def test_socket_fill_lands_on_the_tape(S):
    from polytrader.feeds import clob as clobmod

    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.tokens = {"up": "U", "dn": "D"}
    S.flow = []

    clobmod.handle_clob({
        "event_type": "last_trade_price", "asset_id": "U",
        "price": "0.5", "size": "10.0", "timestamp": "1793035900100",
    })

    assert [(f["side"], f["usd"]) for f in S.flow] == [("up", 5.0)]


def test_socket_fill_for_unknown_token_is_ignored(S):
    from polytrader.feeds import clob as clobmod

    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.tokens = {"up": "U", "dn": "D"}
    S.flow = []

    clobmod.handle_clob({
        "event_type": "last_trade_price", "asset_id": "ZZZ",
        "price": "0.5", "size": "10.0", "timestamp": "1793035900100",
    })
    clobmod.handle_clob({"event_type": "last_trade_price"})

    assert S.flow == []


def test_socket_and_rest_rows_dedupe_to_one_fill(S, monkeypatch):
    flowmod._seen.clear()
    S.ws_start = 1793035800
    S.cond = "0xcond"
    S.tokens = {"up": "U", "dn": "D"}
    S.flow = []
    monkeypatch.setattr(polymod, "recent_trades", lambda cond: [
        {"ts": 1793035900.0, "side": "up", "usd": 7.07, "px": 0.99},
    ])

    asyncio.run(flowmod._poll())
    from polytrader.feeds import clob as clobmod
    clobmod.handle_clob({
        "event_type": "last_trade_price", "asset_id": "U",
        "price": "0.99", "size": "7.14", "timestamp": "1793035900752",
    })

    assert [(f["side"], f["usd"]) for f in S.flow] == [("up", 7.07)]
