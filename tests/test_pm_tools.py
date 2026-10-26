"""Data access: window math, token parsing, book and market normalisation."""
from __future__ import annotations

import json

import pytest

import pm_tools
import trader


def test_window_slug_is_deterministic():
    slug, start, end = pm_tools.btc5m_window(1_000_001)
    assert start == 999_900
    assert end == 1_000_200
    assert slug == "btc-updown-5m-999900"


def test_json_fields_arrive_as_strings_from_gamma():
    assert pm_tools._json_field('["Up", "Down"]') == ["Up", "Down"]
    assert pm_tools._json_field(["Up"]) == ["Up"]
    assert pm_tools._json_field("not json") == "not json"


def test_tokens_are_mapped_to_up_and_dn():
    assert trader._parse_tokens(
        {"outcomes": ["Up", "Down"], "token_ids": ["111", "222"]}
    ) == {"up": "111", "dn": "222"}
    assert trader._parse_tokens(
        {"outcomes": ["Yes", "No"], "token_ids": ["111", "222"]}
    ) == {"up": "111", "dn": "222"}


def test_market_normalises_json_encoded_gamma_fields(monkeypatch):
    raw = {
        "slug": "btc-updown-5m-999900",
        "question": "BTC up?",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.52", "0.48"]',
        "closed": False,
        "conditionId": "0xcond",
        "volume": "1000",
        "liquidity": "50",
        "endDate": "2026-10-26T00:05:00Z",
        "description": "x" * 900,
    }
    monkeypatch.setattr(pm_tools, "_get_retry", lambda *a, **k: [raw])

    m = pm_tools.market("btc-updown-5m-999900")
    assert m["outcomes"] == ["Up", "Down"]
    assert m["token_ids"] == ["111", "222"]
    assert m["condition_id"] == "0xcond"
    assert len(m["description"]) == 600


def test_market_falls_back_to_the_event_slug(monkeypatch):
    calls = []

    def fake(url, *a, **k):
        calls.append(url)
        if "/markets" in url:
            return []
        return [{"markets": [{"slug": "m", "outcomes": '["Up","Down"]',
                              "clobTokenIds": '["1","2"]', "closed": False}]}]

    monkeypatch.setattr(pm_tools, "_get_retry", fake)
    m = pm_tools.market("some-event")
    assert m["token_ids"] == ["1", "2"]
    assert any("/events" in u for u in calls)


def test_book_reduces_to_best_bid_ask_and_depth(monkeypatch):
    raw = {
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}],
        "asks": [{"price": "0.46", "size": "7"}, {"price": "0.44", "size": "3"}],
    }
    monkeypatch.setattr(pm_tools, "_get_retry", lambda *a, **k: raw)

    b = pm_tools.book("TOK")
    assert b["best_bid"] == 0.42 and b["bid_size"] == 5
    assert b["best_ask"] == 0.44 and b["ask_size"] == 3
    assert b["spread"] == pytest.approx(0.02)
    assert b["mid"] == pytest.approx(0.43)
    # depth within 3c of the best on each side
    assert b["bid_depth_3c"] == 15
    assert b["ask_depth_3c"] == 10
    assert b["n_bid_levels"] == 2 and b["n_ask_levels"] == 2


def test_empty_book_has_no_prices(monkeypatch):
    monkeypatch.setattr(pm_tools, "_get_retry", lambda *a, **k: {"bids": [], "asks": []})
    b = pm_tools.book("TOK")
    assert b["best_bid"] is None and b["best_ask"] is None
    assert b["spread"] is None and b["mid"] is None


def test_research_helpers_swallow_transport_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("403 from here")

    monkeypatch.setattr(pm_tools, "_get_retry", boom)
    assert pm_tools.trades("0xcond") == []
    assert pm_tools.wallet_activity("0xaddr") == []
    assert pm_tools.price_history("TOK") == []
