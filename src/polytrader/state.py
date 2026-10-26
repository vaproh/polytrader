"""Shared mutable state and the small helpers that read it."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .config import PRESETS, load_cfg


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    last_trade: float | None = None

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def bid_size(self):
        b = self.best_bid()
        return self.bids.get(b, 0.0) if b else 0.0

    def ask_size(self):
        a = self.best_ask()
        return self.asks.get(a, 0.0) if a else 0.0

    def depth(self, band=0.03):
        b = self.best_bid()
        a = self.best_ask()
        db = sum(s for p, s in self.bids.items() if b and abs(p - b) <= band)
        da = sum(s for p, s in self.asks.items() if a and abs(p - a) <= band)
        return db, da


@dataclass
class Position:
    side: str = ""
    qty: float = 0.0
    entry: float = 0.0
    ts: float = 0.0
    stake: float = 0.0
    locked: bool = False     # breakeven reached -> stop sits at entry

    def clear(self):
        self.side = ""
        self.qty = 0.0
        self.entry = 0.0
        self.ts = 0.0
        self.stake = 0.0
        self.locked = False


class State:
    def __init__(self):
        self.btc = 0.0
        self.btc_hist = deque(maxlen=30000)     # (ts, price); ~120s at 250 tick/s
        self.books: dict[str, Book] = {}        # asset_id -> Book
        self.slug = ""
        self.ws_start = 0
        self.tokens: dict[str, str] = {}        # "up"/"dn" -> asset_id
        self.mid_hist = deque(maxlen=400)       # (250ms bucket, mid_up)
        self.pos = Position()
        self.trades: list[dict] = []
        self.paused = False
        self.live = False
        self.stake = 5.0
        self.regime = "?"
        self.rates = {}
        self.metrics = {}
        self.msg = "starting"
        self.window_loading = False
        self.prefetch = {}
        self.pnl_session = 0.0
        self.warm = 0.0
        self.subscribed: set = set()
        self.last_event = 0.0
        self.ws_connected = False
        self.stop_on = True
        self.cvd = 0.0
        self.cvd_hist = deque(maxlen=4000)
        self.trade_hist = deque(maxlen=20000)
        self.vol_buy = 0.0
        self.vol_sell = 0.0
        self.strike = 0.0        # BTC when the current window opened
        self.twap = 0.0          # 60s BTC mean, the resolution statistic
        self.twap_src = ""       # "chainlink" (exact) or "proxy" (Binance)
        self.twap_ts = 0.0       # last successful exact fetch
        self.usdt_usd = 0.9985   # USDT/USD basis for Binance -> USD
        self.usdt_usd_ts = 0.0
        self.session_open = 0.0  # first BTC price seen by this process
        self.klines: list[float] = []
        self.bar_ts = 0
        self.preset = "SCALP"
        self.cfg = load_cfg()
        if self.cfg.get("preset") in PRESETS:
            self.preset = self.cfg["preset"]
        try:
            stake = float(self.cfg.get("stake", self.stake))
            if stake > 0:
                self.stake = stake
        except (TypeError, ValueError):
            pass
        self.book_dirty = False
        self.last_book_paint = 0.0
        self.broker = None
        self.busy = False
        self.exit_retry_ts = 0.0
        self.prefetch_inflight: set = set()
        self.balance = None
        self.funder = ""


S = State()


def cur_book(side):
    aid = S.tokens.get(side)
    return S.books.get(aid) if aid else None


def secs_left():
    return max(0.0, (S.ws_start + 300) - time.time()) if S.ws_start else 0.0


def mark_price(side):
    bk = cur_book(side)
    if not bk:
        return None
    return bk.best_bid()
