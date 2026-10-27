"""Kline resync: closed bars track the official Binance closes."""
from __future__ import annotations

from polytrader.feeds.binance import _resync_klines


def test_resync_replaces_closed_bars_but_keeps_the_forming_one():
    official = [80000.0 + i for i in range(30)]

    assert _resync_klines(official, 80100.0) == official[:-1] + [80100.0]


def test_resync_falls_back_to_the_official_forming_close():
    official = [80000.0 + i for i in range(30)]

    assert _resync_klines(official, 0.0)[-1] == official[-1]


def test_resync_ignores_an_unusable_response():
    assert _resync_klines([], 80100.0) is None
    assert _resync_klines([80100.0], 80100.0) is None


def test_resync_caps_the_series_at_200_bars():
    official = [80000.0 + i for i in range(300)]

    assert len(_resync_klines(official, 80300.0)) == 200
