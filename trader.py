#!/usr/bin/env python3
"""
trader.py -- the TUI. One screen, keybinds, no GUI.

What it does
  * Binance BTC spot over WebSocket (aggTrade)
  * Polymarket CLOB book over WebSocket (market channel, book + price_change)
  * Detector: rolling regime state (travel, efficiency, drift, reversals)
  * Base rates for the detected state -- measured, not invented
  * Position tracking with live mark-to-market P&L
  * Buy/sell on a keypress. PAPER by default; --live needs explicit creds.

What it deliberately does NOT do
  * It never says "buy now". It describes the state and shows the base rate.
    Every model trained to emit a directional call failed out-of-sample
    (6 period splits, top-5% trimmed -> negative in 5 of 6). The detector
    describes; you decide.

Keys
  u / d     buy UP / DOWN at the ask
  s         sell the open position at the bid
  x         flatten and cancel
  1..9      stake size ($1..$9)
  o         cycle stake up
  p         pause / resume the detector
  r         force window reload
  L         toggle paper/live (live requires --live at launch)
  q         quit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_tools as pm

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LOOKBACK = 36.0
STOP_PCT = 0.20          # default stop (the level the stop study landed on)

# Exit profiles. stop/tp/be are fractions of entry; 0 disables that leg.
#   stop  sell if mark falls this far below entry
#   tp    sell if mark rises this far above entry
#   be    once mark is up this much, raise the stop to breakeven
PRESETS = {
    "SCALP":  {"stop": 0.20, "tp": 0.50, "be": 0.30},
    "TIGHT":  {"stop": 0.15, "tp": 0.30, "be": 0.20},
    "RUNNER": {"stop": 0.25, "tp": 1.00, "be": 0.40},
    "FREE":   {"stop": 0.00, "tp": 0.00, "be": 0.00},
}
CFG_PATH = os.path.expanduser("~/.polytrader.json")


def load_cfg():
    try:
        with open(CFG_PATH) as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_cfg(d):
    try:
        with open(CFG_PATH, "w") as fh:
            json.dump(d, fh)
        os.chmod(CFG_PATH, 0o600)
    except Exception:
        pass
HIST = 240.0

# measured on 19,857 moments (Aug books). conditional on regime, not a buy call.
BASE_RATES = {
    # Measured on the Oct 3-6 250ms capture: 363,397 observations.
    # r20/r50 = P(bid reaches +20%/+50% within 60s). med = median 60s return.
    # These are observations, not settings: do not hand-tune them.
    "DEAD":      {"r20": 0.086, "r50": 0.086, "mean": -0.196, "med": -0.200, "n": 884},
    "QUIET-UP":  {"r20": 0.126, "r50": 0.028, "mean": -0.013, "med": +0.043, "n": 21309},
    "CHOP":      {"r20": 0.281, "r50": 0.108, "mean": +0.019, "med": +0.044, "n": 140799},
    "CHOP-DOWN": {"r20": 0.308, "r50": 0.192, "mean": +0.124, "med": -0.154, "n": 130178},
    "VOLATILE":  {"r20": 0.234, "r50": 0.136, "mean": +0.112, "med": -0.149, "n": 54471},
    "TREND":     {"r20": 0.129, "r50": 0.078, "mean": +0.219, "med": +0.011, "n": 15756},
}


# --------------------------------------------------------------------- state
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
        self.stake = 5
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
        self.session_open = 0.0  # first BTC price seen by this process
        self.klines: list[float] = []
        self.bar_ts = 0
        self.preset = "SCALP"
        self.cfg = load_cfg()
        if self.cfg.get("preset") in PRESETS:
            self.preset = self.cfg["preset"]
        self.stake = int(self.cfg.get("stake", self.stake))
        self.book_dirty = False
        self.last_book_paint = 0.0
        self.broker = None
        self.busy = False
        self.exit_retry_ts = 0.0
        self.prefetch_inflight: set = set()
        self.balance = None
        self.funder = ""


S = State()


# ------------------------------------------------------------------ detector
def recompute_detector():
    """Regime from 250ms buckets, recomputed at 30 Hz.

    Ticks land continuously (hundreds/sec) and overwrite the current bucket, so
    the last value is always the newest price. Math runs over ~144 buckets, not
    over thousands of ticks, which is what keeps this cheap at 30 Hz.
    """
    if len(S.mid_hist) < 4:
        S.regime = "WARMUP"
        S.rates = {}
        return
    now_b = S.mid_hist[-1][0]
    look = int(LOOKBACK * 4)                       # 36s -> 144 buckets
    pts = [(b, m) for b, m in S.mid_hist if b >= now_b - look]
    span = (pts[-1][0] - pts[0][0]) / 4.0 if pts else 0.0
    S.warm = min(1.0, span / (LOOKBACK * 0.85))
    if len(pts) < 6 or span < LOOKBACK * 0.85:
        S.regime = "WARMUP"
        S.rates = {}
        S.metrics = {"travel": float("nan"), "net": float("nan"),
                     "eff": float("nan"), "revs": 0.0, "drift": 0.0,
                     "span": span}
        return

    mid = np.fromiter((m for _, m in pts), dtype=float, count=len(pts))
    d = np.diff(mid)
    travel = float(np.abs(d).sum())
    net = float(mid[-1] - mid[0])
    eff = abs(net) / travel if travel > 1e-12 else 1.0
    revs = int(np.sum(np.sign(d[1:]) != np.sign(d[:-1]))) if len(d) > 1 else 0
    drift = net / mid[0] if mid[0] else 0.0

    if travel < 0.02:
        reg = "DEAD"
    elif eff >= 0.70:
        reg = "TREND"
    elif eff < 0.35:
        reg = "CHOP-DOWN" if net < 0 else "CHOP"
    elif travel >= 0.40:
        reg = "VOLATILE"
    else:
        reg = "QUIET-UP" if net > 0 else "VOLATILE"

    S.regime = reg
    S.rates = BASE_RATES.get(reg, {})
    S.metrics = {"travel": travel, "net": net, "eff": eff, "revs": revs,
                 "drift": drift, "span": span}


def rsi_last(closes, period=14):
    """Wilder RSI on a list of closes. Returns 50.0 until there is enough data."""
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return 50.0
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:period].mean()
    ad = dn[:period].mean()
    for i in range(period, len(d)):
        au = (au * (period - 1) + up[i]) / period
        ad = (ad * (period - 1) + dn[i]) / period
    if ad <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def btc_spark(width=44, lookback=180.0):
    """ASCII sparkline of recent BTC ticks, for context only."""
    if len(S.btc_hist) < 4:
        return ""
    now = S.btc_hist[-1][0]
    pts = [p for t, p in S.btc_hist if t >= now - lookback]
    if len(pts) < 4:
        return ""
    step = max(1, len(pts) // width)
    pts = pts[::step][-width:]
    # 3-point smoothing: raw last-trade prices bounce between bid and ask and
    # an unsmoothed spark looks like a square wave rather than a price path
    if len(pts) >= 5:
        sm = []
        for i in range(len(pts)):
            lo_i, hi_i = max(0, i - 1), min(len(pts), i + 2)
            sm.append(sum(pts[lo_i:hi_i]) / (hi_i - lo_i))
        pts = sm
    lo, hi = min(pts), max(pts)
    if hi - lo < 1e-9:
        return "\u2500" * len(pts)
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    return "".join(blocks[min(7, int((p - lo) / (hi - lo) * 7.999))] for p in pts)


def fetch_klines(limit=120):
    """Seed 1m closes from Binance so RSI(14) is valid immediately."""
    try:
        import urllib.request
        u = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             f"&interval=1m&limit={limit}")
        raw = urllib.request.urlopen(u, timeout=12).read()
        return [float(r[4]) for r in json.loads(raw.decode())]
    except Exception:
        return []


def vol_window(sec):
    """(buy volume, sell volume, trade count) over the last `sec` seconds."""
    now = time.time()
    b = sv = n = 0.0
    for t, q, is_buy in reversed(S.trade_hist):
        if t < now - sec:
            break
        if is_buy:
            b += q
        else:
            sv += q
        n += 1
    return b, sv, int(n)


def warnings():
    """Live flags. Every one is mechanical; each carries its own evidence."""
    out = []
    if len(S.klines) < 30:
        return out
    c = np.asarray(S.klines, dtype=float)
    up_ratio = None

    # --- RSI across three periods
    for n in (7, 14, 25):
        r = rsi_series_py(c, n)
        if r is None:
            continue
        if r >= 80:
            out.append(("red", f"RSI{n} {r:.0f} overbought"))
        elif r >= 70:
            out.append(("yellow", f"RSI{n} {r:.0f} hot"))
        elif r <= 20:
            out.append(("green", f"RSI{n} {r:.0f} oversold"))
        elif r <= 30:
            out.append(("cyan", f"RSI{n} {r:.0f} cold"))

    # --- volume: aggregate + aggressor split
    b30, s30, n30 = vol_window(30)
    b120, s120, _ = vol_window(120)
    if n30 >= 8:
        tot30, tot120 = b30 + s30, b120 + s120
        if tot120 > 0 and tot30 > 0:
            # 30s is a quarter of 120s, so 25% would be "normal"
            share = tot30 / tot120
            if share > 0.55 and tot30 > 1.0:
                out.append(("yellow", f"volume surge {share*100:.0f}% of 2m in 30s"))
        if tot30 > 0:
            up_ratio = b30 / tot30
            if up_ratio >= 0.70:
                out.append(("green", f"aggressive BUYING {up_ratio*100:.0f}% of 30s vol"))
            elif up_ratio <= 0.30:
                out.append(("red", f"aggressive SELLING {(1-up_ratio)*100:.0f}% of 30s vol"))

    # --- CVD direction over 60s
    if len(S.cvd_hist) > 20:
        now = S.cvd_hist[-1][0]
        pts = [v for t, v in S.cvd_hist if t >= now - 60]
        if len(pts) > 5:
            d = pts[-1] - pts[0]
            if abs(d) > 0:
                tag = "green" if d > 0 else "red"
                out.append((tag, f"CVD {'+' if d > 0 else ''}{d:.1f} BTC / 60s"))

    # --- divergence: price new high, RSI not
    r14 = rsi_series_py(c, 14)
    if r14 is not None and len(c) >= 40:
        px_now, px_prev = c[-1], c[-20:].max()
        if px_now >= px_prev * 0.9995:
            r_prev = rsi_series_py(c[:-15], 14)
            if r_prev is not None and r14 < r_prev - 3:
                out.append(("yellow", "bearish divergence (price high, RSI lower)"))
        px_lo = c[-20:].min()
        if px_now <= px_lo * 1.0005:
            r_prev = rsi_series_py(c[:-15], 14)
            if r_prev is not None and r14 > r_prev + 3:
                out.append(("yellow", "bullish divergence (price low, RSI higher)"))

    # --- volatility regime shift
    if len(c) >= 60:
        r = np.diff(np.log(c))
        sv = np.std(r[-15:])
        lv = np.std(r[-60:])
        if lv > 0:
            ratio = sv / lv
            if ratio > 1.8:
                out.append(("yellow", f"vol expansion x{ratio:.1f}"))
            elif ratio < 0.45:
                out.append(("dim", f"vol compression x{ratio:.1f}"))

    # --- trend context vs the 200-period mean
    if len(c) >= 200:
        e200 = float(pd.Series(c).ewm(span=200).mean().iloc[-1])
        gap = (c[-1] / e200 - 1) * 100
        if gap > 0.15:
            out.append(("green", f"above EMA200 by {gap:.2f}%"))
        elif gap < -0.15:
            out.append(("red", f"below EMA200 by {abs(gap):.2f}%"))

    # --- MACD cross within the last few bars
    if len(c) >= 40:
        ser = pd.Series(c)
        macd = ser.ewm(span=12).mean() - ser.ewm(span=26).mean()
        sigl = macd.ewm(span=9).mean()
        diff = (macd - sigl).values
        if len(diff) > 3:
            if diff[-1] > 0 >= diff[-2] and abs(diff[-1]) > 0:
                out.append(("green", "MACD crossed UP"))
            elif diff[-1] < 0 <= diff[-2]:
                out.append(("red", "MACD crossed DOWN"))

    # --- Bollinger squeeze: a coiled range resolves violently
    if len(c) >= 40:
        ser = pd.Series(c)
        ma = ser.rolling(20).mean()
        sd = ser.rolling(20).std()
        width = (2 * sd / ma).dropna()
        if len(width) > 25:
            pct = float((width.iloc[-1] > width.iloc[-25:]).mean())
            if pct <= 0.15:
                out.append(("yellow", "Bollinger squeeze -- range coiled"))

    # --- streak
    if len(c) >= 6:
        d = np.diff(c[-7:])
        if (d > 0).all():
            out.append(("yellow", f"{len(d)} consecutive up bars"))
        elif (d < 0).all():
            out.append(("yellow", f"{len(d)} consecutive down bars"))

    return out


def rsi_series_py(c, n=14):
    if len(c) < n + 2:
        return None
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:n].mean()
    ad = dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad <= 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + au / ad))


def btc_sigma(lookback=120.0):
    """Per-SECOND volatility from ticks, not from 1m bars.

    A 5-minute window needs second-scale vol. Using 30 one-minute bars measures
    the last half hour's regime, which is the wrong timescale -- BTC can go from
    dead to violent inside a single window.
    """
    if len(S.btc_hist) < 20:
        return None
    now = S.btc_hist[-1][0]
    buckets = {}
    for t, p in S.btc_hist:
        if t >= now - lookback and p > 0:
            buckets[int(t)] = p          # last price in each whole second
    if len(buckets) < 20:
        return None
    ks = sorted(buckets)
    px = np.array([buckets[k] for k in ks], dtype=float)
    r = np.diff(np.log(px))
    r = r[np.isfinite(r)]
    if len(r) < 15:
        return None
    sd = float(np.std(r))
    if sd <= 0:
        return None
    # FLOOR: a quiet two minutes is not a low-volatility regime. If tick vol
    # comes in far below the hour-scale vol, trust the longer measure -- else a
    # one-cent wiggle produces an absurdly confident P(up).
    floor = None
    if len(S.klines) >= 20:
        bar = np.diff(np.log(np.asarray(S.klines[-31:], dtype=float)))
        bar = bar[np.isfinite(bar)]
        if len(bar) >= 5:
            floor = float(np.std(bar) / np.sqrt(60.0))
    if floor and floor > sd:
        sd = floor
    return sd


def btc_state():
    """Everything the window depends on, expressed in BTC terms."""
    sig = btc_sigma()
    if sig is None or not S.strike or S.btc <= 0:
        return None
    left = max(1.0, secs_left())
    dist = S.btc - S.strike
    # how many standard deviations of expected travel away from the strike
    move_sd = sig * np.sqrt(left)
    z = float(np.log(S.btc / S.strike) / move_sd) if move_sd > 0 else 0.0
    # expected absolute travel over the remaining time, in dollars
    travel = S.btc * move_sd
    # momentum over the last 30s / 60s of ticks
    now = S.btc_hist[-1][0] if S.btc_hist else 0
    def chg(sec):
        pts = [p for t, p in S.btc_hist if t >= now - sec]
        return (S.btc - pts[0]) if pts else 0.0
    return {"sig": sig, "left": left, "dist": dist, "z": z,
            "travel": travel, "m30": chg(30), "m60": chg(60),
            "p": 0.5 * (1.0 + __import__("math").erf(z / np.sqrt(2.0)))}


def fair_p():
    """P(this window resolves UP), from BTC alone.

    Barrier probability under a driftless lognormal walk:
        P(up) = Phi( ln(S/K) / (sigma * sqrt(T)) )
    K = BTC when the window opened, S = BTC now, sigma = realised per-second
    vol from the last 30 one-minute bars, T = seconds left.

    Honest status, measured on 481 resolved windows at 60s left:
        model  Brier 0.177
        market Brier 0.082
    The market is more than twice as well calibrated, so this is NOT an edge --
    betting against the market because the model disagrees loses. It is a
    fair-value readout: what the window is worth if BTC behaves like a
    random walk with recent volatility.
    """
    if not S.strike or S.btc <= 0 or len(S.klines) < 20:
        return None
    st = btc_state()
    if st is None:
        return None
    sig = st["sig"]
    left = max(1.0, secs_left())
    try:
        z = float(np.log(S.btc / S.strike) / (sig * np.sqrt(left)))
    except (ValueError, ZeroDivisionError):
        return None
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def gate_checks():
    """The original signal-bot's entry rules, evaluated live as pass/fail.

    Volatility >= 0.40, efficiency < 0.35, drift <= +2c, price 0.03-0.95,
    40-250s to close. They are shown because they are the user's own rules --
    NOT because they are profitable. On the October capture the full set scored
    -6.0% per trade; volatility alone was the only gate with any signal.
    """
    m = S.metrics or {}
    tr = m.get("travel", float("nan"))
    ef = m.get("eff", float("nan"))
    dr = m.get("drift", float("nan"))
    bu = cur_book("up")
    px = bu.best_ask() if bu else None
    sl = secs_left()
    return [
        ("vol", tr >= 0.40, "—" if tr != tr else f"{tr:.2f}"),
        ("eff", ef < 0.35, "—" if ef != ef else f"{ef:.2f}"),
        ("drift", dr <= 0.02, "—" if dr != dr else f"{dr*100:+.0f}%"),
        ("px", px is not None and 0.03 <= px <= 0.95, "—" if px is None else f"{px:.2f}"),
        ("secs", 40 <= sl <= 250, f"{sl:.0f}s"),
    ]


def cur_book(side):
    aid = S.tokens.get(side)
    return S.books.get(aid) if aid else None



def make_books_text():
    b = Text()
    b.append(f"{S.slug}", style="bold")
    b.append(f"   resolves in {secs_left():.0f}s\n\n")
    for side, label in (("up", "UP  "), ("dn", "DOWN")):
        bk = cur_book(side)
        if not bk or bk.best_bid() is None:
            b.append(f"{label}  (no book)\n", style="dim")
            continue
        bb, ba = bk.best_bid(), bk.best_ask()
        db, da = bk.depth()
        b.append(f"{label}  ", style="bold white")
        b.append(f"bid {bb:.2f}" if bb is not None else "bid  --", style="green")
        b.append(f"   ask {ba:.2f}" if ba is not None else "   ask  --", style="red")
        b.append(f"   q {bk.bid_size():>5.0f}/{bk.ask_size():>5.0f}", style="white")
        b.append(f"   depth3c ${db*(bb or 0):>6.0f}", style="dim")
        b.append(f"   size@ask ${(ba or 0)*bk.ask_size():>6.0f}\n", style="dim")
    bu, bd = cur_book("up"), cur_book("dn")
    if bu and bd and bu.best_bid() and bd.best_bid():
        ps = bu.best_bid() + bd.best_bid()
        b.append(f"\npair sum {ps:.4f}", style="yellow")
        b.append(f"   you pay {abs(1-ps)*100:.2f}c/contract vs 1.0000",
                 style="dim")

    return b


def secs_left():
    return max(0.0, (S.ws_start + 300) - time.time()) if S.ws_start else 0.0


def on_price_tick(p, now):
    """Fold one trade price into the spot history and the 1m close series.

    A new minute appends the close of the bar that just ended; every tick
    overwrites the forming bar's close with the live price. Without the append
    the series never grows, so RSI, EMA200, MACD and the vol floor all keep
    reading the bars that were seeded at startup.
    """
    b = int(now // 60)
    if b != S.bar_ts:
        if S.bar_ts and S.klines:
            S.klines[-1] = S.btc or p     # freeze the bar that just ended
            S.klines.append(p)            # open the next one
            if len(S.klines) > 200:
                S.klines = S.klines[-200:]
        S.bar_ts = b
    S.btc = p
    if not S.session_open:
        S.session_open = p
    S.btc_hist.append((now, p))
    if not S.klines:            # seed failed: grow from ticks instead of staying dead
        S.klines = [p]
    S.klines[-1] = p


# ------------------------------------------------------------------- feeds
async def binance_loop():
    import websockets
    while True:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as w:
                S.msg = "binance connected"
                async for raw in w:
                    m = json.loads(raw)
                    p = float(m.get("p", 0))
                    q = float(m.get("q", 0) or 0)
                    # "m" = buyer is the maker, so the taker SOLD into the bid
                    if q > 0:
                        S.vol_buy += 0.0 if m.get("m") else q
                        S.vol_sell += q if m.get("m") else 0.0
                        S.cvd += (-q if m.get("m") else q)
                        S.cvd_hist.append((time.time(), S.cvd))
                        S.trade_hist.append((time.time(), q, 0.0 if m.get("m") else 1.0))
                        cut = time.time() - 300
                        while S.trade_hist and S.trade_hist[0][0] < cut:
                            S.trade_hist.popleft()
                    if p:
                        on_price_tick(p, time.time())
        except Exception as e:
            S.msg = f"binance: {str(e)[:40]}"
            await asyncio.sleep(2)


async def clob_loop():
    import websockets
    while True:
        ids = list(S.tokens.values())
        if not ids:
            await asyncio.sleep(0.5)
            continue
        try:
            async with websockets.connect(CLOB_WS, ping_interval=20,
                                          max_size=2 ** 22) as w:
                await w.send(json.dumps({"assets_ids": ids, "type": "market"}))
                S.subscribed = set(ids)
                S.ws_connected = True
                S.msg = "clob connected"
                while True:
                    # Check for a rolled window on a TIMER, not on message
                    # arrival. A subscription to a dead window goes silent, so
                    # a message-driven check would block here forever and the
                    # socket would never resubscribe.
                    if set(S.tokens.values()) != S.subscribed:
                        break
                    try:
                        raw = await asyncio.wait_for(w.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict):
                        msgs = [msgs]
                    for m in msgs:
                        handle_clob(m)
        except Exception as e:
            S.ws_connected = False
            S.msg = f"clob: {str(e)[:40]}"
        S.ws_connected = False
        await asyncio.sleep(0.3)


def _record_tick():
    """Push the current mid onto the series on every book-changing message."""
    bu, bd = S.books.get(S.tokens.get("up", "")), S.books.get(S.tokens.get("dn", ""))
    if bu and bd and bu.best_bid() is not None and bd.best_bid() is not None:
        mid = (bu.best_bid() + (bu.best_ask() or bu.best_bid())) / 2
        b = int(time.time() * 4)          # 250ms bucket index
        if S.mid_hist and S.mid_hist[-1][0] == b:
            S.mid_hist[-1] = (b, mid)     # tick updates the CURRENT bucket
        else:
            S.mid_hist.append((b, mid))


def handle_clob(m):
    S.last_event = time.time()
    now_p = S.last_event
    _record_tick()
    if now_p - S.last_book_paint >= 0.04:
        S.last_book_paint = now_p
        S.book_dirty = True
    et = m.get("event_type") or m.get("type")
    aid = m.get("asset_id")
    if not aid:
        return
    bk = S.books.setdefault(aid, Book())
    if et == "book":
        bk.bids = {float(x["price"]): float(x["size"]) for x in m.get("bids", [])
                   if float(x["size"]) > 0}
        bk.asks = {float(x["price"]): float(x["size"]) for x in m.get("asks", [])
                   if float(x["size"]) > 0}
    elif et == "price_change":
        for c in m.get("changes", []):
            p, sz = float(c["price"]), float(c["size"])
            side = str(c.get("side", "")).upper()
            tgt = bk.bids if side == "BUY" else bk.asks
            if sz <= 0:
                tgt.pop(p, None)
            else:
                tgt[p] = sz
    elif et == "last_trade_price":
        try:
            bk.last_trade = float(m.get("price"))
        except Exception:
            pass


async def rest_fallback_loop():
    """Fill in from REST whenever the websocket has been quiet for >3s."""
    while True:
        await asyncio.sleep(0.5)
        if not S.tokens:
            continue
        # the socket being quiet is NOT the same as the socket being dead.
        # only poll when it is down, or truly silent for a long stretch.
        if S.ws_connected and time.time() - S.last_event < 8.0:
            continue
        for side, aid in list(S.tokens.items()):
            try:
                b = await asyncio.to_thread(pm.book, aid)
            except Exception:
                continue
            if not b:
                continue
            bk = S.books.setdefault(aid, Book())
            ba, bb = b.get("best_ask"), b.get("best_bid")
            if ba is None or bb is None:
                continue
            if not bk.bids:
                bk.bids = {bb: b.get("bid_size") or 1.0}
            if not bk.asks:
                bk.asks = {ba: b.get("ask_size") or 1.0}
        if not S.ws_connected:
            S.msg = "clob: REST fallback"


def _parse_tokens(m):
    toks = dict(zip([o.lower() for o in m["outcomes"]], m["token_ids"]))
    return {"up" if k in ("up", "yes") else "dn": v for k, v in toks.items()}


async def _prefetch(ws_next):
    """Warm the token cache for the NEXT window before it starts."""
    if ws_next in S.prefetch or ws_next in S.prefetch_inflight:
        return
    S.prefetch_inflight.add(ws_next)
    try:
        m = await asyncio.to_thread(pm.market, f"btc-updown-5m-{ws_next}")
        if m:
            S.prefetch[ws_next] = _parse_tokens(m)
    except Exception:
        pass
    finally:
        S.prefetch_inflight.discard(ws_next)


async def window_loop():
    """Roll to the current 5-min window. Prefetches so the switch is instant."""
    while True:
        now = time.time()
        ws = int(now // 300 * 300)
        left = 300 - (now - ws)

        # warm the next window ahead of time so rollover costs no round-trip
        if left <= 20:
            asyncio.create_task(_prefetch(ws + 300))

        if not S.window_loading and ws != S.ws_start:
            S.window_loading = True
            try:
                toks = S.prefetch.pop(ws, None)
                if toks is None:
                    m = await asyncio.to_thread(pm.market, f"btc-updown-5m-{ws}")
                    toks = _parse_tokens(m) if m else None
                if toks:
                    note = ""
                    fill_px = None
                    if S.pos.side and S.live and S.broker and S.broker.ok:
                        # best effort: close before the book for this window goes away.
                        # shortcut: if this sell fails the position is still booked at
                        # the last bid; reconcile it on polymarket.com.
                        px = mark_price(S.pos.side) or 0.0
                        ok, detail = await asyncio.to_thread(
                            S.broker.sell, S.tokens.get(S.pos.side), S.pos.qty, px)
                        if ok:
                            fill_px = S.broker.last_price or px
                        else:
                            note = f" -- SELL FAILED: {detail[:30]}"
                    roll_to_window(ws, toks, fill_px)
                    S.msg += note
            except Exception as e:
                S.msg = f"window: {str(e)[:40]}"
            S.window_loading = False

        # wake exactly on the boundary instead of up to 1s late
        now = time.time()
        nxt = (int(now // 300) + 1) * 300
        await asyncio.sleep(max(0.05, min(1.0, nxt - now + 0.01)))


def mark_price(side):
    bk = cur_book(side)
    if not bk:
        return None
    return bk.best_bid()


def settle_position(reason="", px=None, qty=None):
    """Book a close, or a partial close when `qty` is given."""
    if not S.pos.side:
        return
    if px is None:
        px = mark_price(S.pos.side) or 0.0
    q = S.pos.qty if qty is None else min(float(qty), S.pos.qty)
    if q <= 0:
        return
    pnl = (px - S.pos.entry) * q
    S.pnl_session += pnl
    S.trades.insert(0, {"side": S.pos.side, "entry": S.pos.entry, "exit": px,
                        "qty": q, "pnl": pnl,
                        "pct": (px / S.pos.entry - 1) if S.pos.entry else 0.0,
                        "ts": time.strftime("%H:%M:%S")})
    del S.trades[200:]
    S.pos.qty -= q
    if S.pos.qty <= 1e-9:
        S.pos.clear()
        S.msg = f"closed {reason}"
    else:
        S.msg = f"partial {reason}: {q:.1f}sh @ {px:.3f}"


def roll_to_window(ws, toks, px=None):
    """Switch to a window. Settle any open position at the OLD mark first.

    The books must not be cleared before settling: the position is marked
    against the window that is closing. Clearing first books every open
    position at 0.00, a fake total loss. Pass `px` when a live sell already
    reported a fill price.
    """
    if S.pos.side:
        settle_position("window rolled", px)
    S.tokens = toks
    S.slug = f"btc-updown-5m-{ws}"
    S.ws_start = ws
    S.strike = S.btc or (S.klines[-1] if S.klines else 0.0)
    S.mid_hist.clear()
    S.books.clear()
    S.msg = f"window {ws}"


def exit_decision(entry, mark, preset, stop_on=True, locked=False):
    """Pure exit rule: (reason or None, locked).

    Order matters: breakeven arms first, then TP, then the stop. If TP and the
    stop could both fire on one tick, TP wins -- you were in profit.
    """
    pr = PRESETS.get(preset, PRESETS["SCALP"])
    if pr["be"] > 0 and not locked and mark >= entry * (1 + pr["be"]):
        locked = True
    if pr["tp"] > 0 and mark >= entry * (1 + pr["tp"]):
        return f"TAKE PROFIT +{pr['tp'] * 100:.0f}%", locked
    if not stop_on:
        return None, locked
    if locked:
        if mark <= entry:
            return "BE STOP", locked
        return None, locked
    if pr["stop"] <= 0:
        return None, locked
    if mark <= entry * (1 - pr["stop"]):
        return f"STOP -{pr['stop'] * 100:.0f}%", locked
    return None, locked


# --------------------------------------------------------------------- view
def bar(v, width=12):
    v = max(0.0, min(1.0, v))
    n = int(round(v * width))
    return "█" * n + "·" * (width - n)


class Panel(Static):
    pass


class TraderApp(App):
    CSS = """
    Screen { background: #000000; }
    #top { height: 5; }
    #books { height: 8; }
    #duo { height: 10; }
    #alerts { height: 5; }
    #stats { height: 7; }
    #log { height: 1fr; }
    #regime { width: 1fr; }
    #pos { width: 1fr; }
    .box { border: round #444444; padding: 0 1; }
    """
    BINDINGS = [
        ("k", "toggle_stop", "stop"),
        ("]", "preset_next", "preset+"),
        ("[", "preset_prev", "preset-"),
        ("u", "buy('up')", "buy UP"),
        ("d", "buy('dn')", "buy DOWN"),
        ("s", "sell", "sell"),
        ("x", "flatten", "flatten"),
        ("1", "stake(1)"), ("2", "stake(2)"), ("3", "stake(3)"),
        ("4", "stake(4)"), ("5", "stake(5)"), ("6", "stake(6)"),
        ("7", "stake(7)"), ("8", "stake(8)"), ("9", "stake(9)"),
        ("o", "stake_cycle", "size up"),
        ("p", "pause", "pause"),
        ("r", "reload", "reload"),
        ("L", "toggle_live", "paper/live"),
        ("q", "quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Panel(id="top", classes="box")
        yield Panel(id="books", classes="box")
        with Horizontal(id="duo"):
            yield Panel(id="regime", classes="box")
            yield Panel(id="pos", classes="box")
        yield Panel(id="alerts", classes="box")
        yield Panel(id="stats", classes="box")
        yield Panel(id="log", classes="box")
        yield Footer()

    def on_mount(self):
        asyncio.create_task(self._seed_klines())
        self.set_interval(0.10, self.refresh_view)      # paint: 10 FPS
        self.set_interval(0.033, self.tick_view)        # 30 Hz: detect + paint
        asyncio.create_task(self._boot())

    async def _boot(self):
        asyncio.create_task(binance_loop())
        asyncio.create_task(window_loop())
        asyncio.create_task(rest_fallback_loop())
        await asyncio.sleep(1.5)
        asyncio.create_task(clob_loop())

    # ---- keys
    def action_stake(self, n):
        S.stake = n
        save_cfg({"preset": S.preset, "stake": n})
        S.msg = f"stake ${n}"

    def action_stake_cycle(self):
        S.stake = 10 if S.stake >= 9 else S.stake + 1
        save_cfg({"preset": S.preset, "stake": S.stake})
        S.msg = f"stake ${S.stake}"

    def action_pause(self):
        S.paused = not S.paused
        S.msg = "paused" if S.paused else "running"

    def action_reload(self):
        S.ws_start = 0
        S.msg = "reloading window"

    def action_toggle_live(self):
        if not S.live and (not S.broker or not S.broker.ok):
            S.msg = "no live broker -- staying paper"
            return
        S.live = not S.live
        S.msg = ("LIVE -- orders will be real" if S.live
                 else "paper mode")

    def action_flatten(self):
        if S.live and S.pos.side and S.broker and S.broker.ok:
            bid = mark_price(S.pos.side) or 0.0
            S.busy = True
            S.msg = "FLATTENING"
            asyncio.create_task(self._live_sell(bid))
            self.refresh_view()
            return
        settle_position("flatten")
        self.refresh_view()

    def _set_preset(self, name):
        S.preset = name
        save_cfg({"preset": name, "stake": S.stake})
        pr = PRESETS[name]
        S.msg = (f"preset {name}: stop {pr['stop']*100:.0f}%  "
                 f"tp {pr['tp']*100:.0f}%  be {pr['be']*100:.0f}%")
        self.refresh_view()

    def action_preset_next(self):
        names = list(PRESETS)
        i = names.index(S.preset) if S.preset in names else -1
        self._set_preset(names[(i + 1) % len(names)])

    def action_preset_prev(self):
        names = list(PRESETS)
        i = names.index(S.preset) if S.preset in names else 0
        self._set_preset(names[(i - 1) % len(names)])

    def action_toggle_stop(self):
        S.stop_on = not S.stop_on
        S.msg = f"stop {'ARMED' if S.stop_on else 'OFF'}"
        self.refresh_view()

    def action_buy(self, side):
        if S.pos.side:
            S.msg = f"already {S.pos.side.upper()} -- s to sell first"
            return
        bk = cur_book(side)
        if not bk or not bk.best_ask():
            S.msg = "no ask on that side"
            return
        ask = bk.best_ask()
        if ask <= 0.01 or ask >= 0.99:
            S.msg = f"ask {ask:.2f} outside tradeable band"
            return
        if secs_left() < 20:
            S.msg = "too close to resolution"
            return
        qty = S.stake / ask
        if S.live:
            if not S.broker or not S.broker.ok:
                S.msg = "live order blocked: " + (S.broker.error if S.broker else "no broker")
                return
            if S.busy:
                S.msg = "order in flight -- wait"
                return
            # Do NOT block the render loop on signing + POST. Fire it in the
            # background and paint "SENDING" immediately.
            S.busy = True
            S.msg = f"SENDING {side.upper()} ${S.stake} @ {ask:.3f}"
            asyncio.create_task(self._live_buy(side, ask, qty))
        else:
            S.msg = (f"PAPER BUY {side.upper()} {qty:.1f}sh @ {ask:.3f} (${S.stake})")
            S.pos.side = side
            S.pos.qty = qty
            S.pos.entry = ask
            S.pos.ts = time.time()
            S.pos.stake = S.stake
        self.refresh_view()

    async def _live_buy(self, side, ask, qty):
        tok = S.tokens.get(side)
        try:
            ok, detail = await asyncio.to_thread(S.broker.buy, tok, S.stake, ask)
        except Exception as e:
            ok, detail = False, f"buy error: {str(e)[:60]}"
        S.busy = False
        if not ok:
            S.msg = detail
            return
        S.msg = detail
        filled = getattr(S.broker, "last_fill", 0.0) or qty
        price = getattr(S.broker, "last_price", 0.0) or ask
        S.pos.side = side
        S.pos.qty = filled
        S.pos.entry = price
        S.pos.ts = time.time()
        S.pos.stake = S.stake
        await self._refresh_balance()

    async def _live_sell(self, bid):
        side, qty = S.pos.side, S.pos.qty
        tok = S.tokens.get(side) if side else None
        try:
            ok, detail = await asyncio.to_thread(S.broker.sell, tok, qty, bid)
        except Exception as e:
            ok, detail = False, f"sell error: {str(e)[:60]}"
        S.busy = False
        if not ok:
            S.msg = detail
            return
        S.msg = detail
        px = getattr(S.broker, "last_price", 0.0) or bid
        sold = getattr(S.broker, "last_fill", 0.0) or qty
        settle_position("sold", px, sold)
        await self._refresh_balance()

    async def _refresh_balance(self):
        if not (S.broker and S.broker.ok):
            return
        try:
            bal = await asyncio.to_thread(S.broker.balance)
        except Exception:
            return
        if bal is not None:
            S.balance = bal

    def action_sell(self):
        if not S.pos.side:
            S.msg = "no position"
            return
        if S.live:
            if not S.broker or not S.broker.ok:
                S.msg = "live sell blocked: broker not ready"
                return
            if S.busy:
                S.msg = "order in flight -- wait"
                return
            bid = mark_price(S.pos.side) or 0.0
            S.busy = True
            S.msg = f"SENDING SELL {S.pos.qty:.1f}sh @ {bid:.3f}"
            asyncio.create_task(self._live_sell(bid))
            self.refresh_view()
            return
        settle_position("sold")
        self.refresh_view()

    # ---- render
    _sig = {}

    def _set(self, sel, text):
        """Push to a panel only if its rendered content actually changed."""
        if isinstance(text, Text):
            sig = (text.plain, tuple((sp.start, sp.end, str(sp.style)) for sp in text.spans))
        else:
            sig = str(text)
        if self._sig.get(sel) == sig:
            return False
        self._sig[sel] = sig
        try:
            self.query_one(sel, Panel).update(text)
        except Exception:
            # the screen is being torn down (quit / resize); drop the paint
            return False
        return True

    def _unused_sample_detector(self):
        """Fixed 4 Hz sampling, independent of paint rate.

        `travel` is a sum of absolute moves, so sampling faster inflates it and
        silently re-scales every regime threshold. Keeping this on its own timer
        means the paint rate can change without changing what the detector says.
        """
        if S.paused:
            return
        bu, bd = cur_book("up"), cur_book("dn")
        if bu and bu.best_bid() is not None and bd and bd.best_bid() is not None:
            mid = (bu.best_bid() + (bu.best_ask() or bu.best_bid())) / 2
            # mid_hist is a deque(maxlen=4000): already bounded, no manual trim
            S.mid_hist.append((time.time(), mid))

    def paint_books(self):
        """Repaint just the book panel when new data lands (<=25/s)."""
        if not self.is_mounted or not S.book_dirty:
            return
        S.book_dirty = False
        self._set("#books", make_books_text())

    def _close_at(self, reason, mk):
        """Close at the current mark, live or paper."""
        if S.live and S.broker and S.broker.ok:
            now = time.time()
            # a rejected sell must not retry at 30 Hz; wait two seconds
            if S.busy or now < S.exit_retry_ts:
                return
            S.exit_retry_ts = now + 2.0
            S.busy = True
            S.msg = f"{reason} -- selling at {mk:.3f}"
            asyncio.create_task(self._live_sell(mk))
        else:
            settle_position(reason)
            self.refresh_view()

    async def _seed_klines(self):
        """Pull 1m candles once so RSI(14) is valid from the first frame."""
        S.klines = await asyncio.to_thread(fetch_klines)

    def check_exits(self):
        """Take profit, breakeven lock, hard stop. Evaluated at 30 Hz."""
        if not S.pos.side or not S.pos.entry:
            return
        mk = mark_price(S.pos.side)
        if mk is None:
            return
        reason, S.pos.locked = exit_decision(
            S.pos.entry, mk, S.preset, stop_on=S.stop_on, locked=S.pos.locked)
        if reason:
            self._close_at(reason, mk)

    def tick_view(self):
        """30 Hz: recompute the detector from fresh ticks, paint, enforce stop."""
        if not self.is_mounted:
            return
        if not S.paused:
            recompute_detector()
        self.paint_books()
        self.check_exits()

    def refresh_view(self):
        if not self.is_mounted:
            return
        if not S.paused:
            recompute_detector()

        # top
        btc = S.btc
        chg = ""
        if S.session_open:
            chg = f"{(btc/S.session_open-1)*100:+.2f}% session"
        mode = ("DRY" if (S.broker and getattr(S.broker, "dry_run", False))
                else ("LIVE" if S.live else "PAPER"))
        col = "yellow" if mode == "DRY" else ("red" if S.live else "cyan")
        t = Text()
        t.append(f"BTC ${btc:,.2f}  ", style="bold white")
        t.append(f"{chg}   ", style="dim")
        t.append(f"[{mode}]", style=f"bold {col}")
        if S.live and S.funder:
            t.append(f" {S.funder[:6]}..{S.funder[-4:]}", style="dim")
            if S.balance is not None:
                t.append(f" ${S.balance:.2f}", style="green")
        t.append(f"   stake ${S.stake}", style="yellow")
        t.append(f"   {S.msg}", style="dim")
        st = btc_state()
        if st is not None:
            t.append("\nBTC ", style="dim")
            t.append(f"{st['dist']:+.2f}", style=("green" if st["dist"] >= 0 else "red"))
            t.append(f"  z {st['z']:+.2f}sd", style="dim")
            t.append(f"  vol {st['sig']*1e4:.2f}bp/s", style="dim")
            t.append(f"  travel ${st['travel']:.0f}", style="dim")
            t.append(f"  30s {st['m30']:+.2f}", style=("green" if st["m30"] >= 0 else "red"))
            t.append(f"  60s {st['m60']:+.2f}", style=("green" if st["m60"] >= 0 else "red"))
        fp = fair_p()
        if fp is not None:
            bu = cur_book("up")
            mk_ask = bu.best_ask() if bu else None
            t.append("\nFAIR ", style="dim")
            t.append(f"P(up) {fp*100:3.0f}%", style="bold white")
            if mk_ask:
                gap = fp - mk_ask
                t.append("   mkt ", style="dim")
                t.append(f"{mk_ask:.2f}", style="white")
                t.append("   gap ", style="dim")
                t.append(f"{gap*100:+.0f}c", style="dim")
                # the market is the sharper forecaster (Brier 0.08 vs 0.18), so
                # a gap is evidence the model is wrong, not an opportunity
                t.append("  mkt sharper", style="dim italic")
            t.append(f"   strike ${S.strike:,.0f}" if S.strike else "", style="dim")
        sp = btc_spark()
        if sp or S.klines:
            t.append("\n")
            if S.klines:
                r14 = rsi_last(S.klines)
                col = "red" if r14 >= 70 else ("green" if r14 <= 30 else "white")
                t.append("RSI ", style="dim")
                t.append(f"{r14:3.0f}", style=f"bold {col}")
                t.append("  ", style="dim")
            t.append(sp, style="cyan")
            # what the measurement actually says, so the number is not over-read
            t.append("   measured: RSI<30 next 1m up 48.0% vs 47.9% base", style="dim italic")
        self._set("#top", t)

        self._set("#books", make_books_text())

        # regime
        r = Text()
        reg = S.regime
        style = {"DEAD": "red", "TREND": "red", "CHOP": "green",
                 "CHOP-DOWN": "bright_green", "VOLATILE": "yellow",
                 "QUIET-UP": "dim"}.get(reg, "white")
        r.append("DETECTED STATE\n", style="bold")
        if reg == "WARMUP":
            r.append("WARMUP\n", style="bold yellow")
            r.append(f"need 36s of book history\n", style="dim")
            r.append(f"{bar(getattr(S,'warm',0.0))} {getattr(S,'warm',0)*100:.0f}%\n\n",
                     style="yellow")
        else:
            r.append(f"{reg}\n", style=f"bold {style}")
            br = S.rates or {}
            med = br.get("med")
            if med is not None:
                if med > 0:
                    r.append(f"median {med*100:+.1f}%  most trades up\n", style="green")
                else:
                    r.append(f"median {med*100:+.1f}%  most trades LOSE\n", style="bold red")
            r.append("\n")
            m = S.metrics
            if m:
                r.append(f"travel  {m['travel']:.3f}\n", style="dim")
                r.append(f"effic.  {m['eff']:.2f}  {bar(m['eff'])}\n", style="dim")
                r.append(f"net     {m['net']:+.3f}\n", style="dim")
                r.append(f"revers  {m['revs']:.0f}\n", style="dim")
                r.append(f"drift   {m['drift']*100:+.1f}%\n", style="dim")
        self._set("#regime", r)

        # position
        p = Text()
        p.append("POSITION\n", style="bold")
        if S.pos.side:
            mk = mark_price(S.pos.side) or 0.0
            pnl = (mk - S.pos.entry) * S.pos.qty
            pct = (mk / S.pos.entry - 1) * 100 if S.pos.entry else 0.0
            st = "green" if pnl >= 0 else "red"
            p.append(f"{S.pos.side.upper()}  {S.pos.qty:.1f} sh\n", style="bold")
            p.append(f"entry {S.pos.entry:.3f}\n", style="dim")
            p.append(f"mark  {mk:.3f}\n", style="dim")
            p.append(f"P&L   ${pnl:+.2f} ({pct:+.1f}%)\n", style=f"bold {st}")
            p.append(f"held  {time.time()-S.pos.ts:.0f}s\n", style="dim")
            pr = PRESETS.get(S.preset, PRESETS["SCALP"])
            stop_px = S.pos.entry if S.pos.locked else S.pos.entry * (1 - pr["stop"])
            if not S.stop_on:
                p.append("stop  OFF\n", style="bold yellow")
            elif pr["stop"] <= 0 and not S.pos.locked:
                p.append("stop  none (FREE)\n", style="dim")
            else:
                lbl = "BE stop" if S.pos.locked else "stop"
                near = "bold red" if mk <= stop_px * 1.06 else "red"
                p.append(f"{lbl} {stop_px:.3f}\n", style=near)
            if pr["tp"] > 0:
                tp_px = S.pos.entry * (1 + pr["tp"])
                if tp_px > 0.99:
                    # a +50% target on a 0.78 entry is 1.17 -- it can never fill
                    p.append(f"tp    unreachable\n", style="bold yellow")
                else:
                    p.append(f"tp    {tp_px:.3f} (+{pr['tp']*100:.0f}%)\n", style="green")
        else:
            p.append("flat\n\n", style="dim")
        tot = "green" if S.pnl_session >= 0 else "red"
        p.append(f"session ${S.pnl_session:+.2f}", style=f"bold {tot}")
        if S.trades:
            wins = [t for t in S.trades if t["pnl"] > 0]
            losses = [t for t in S.trades if t["pnl"] <= 0]
            wr = len(wins) / len(S.trades) * 100
            aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
            al = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
            p.append(f"   {len(S.trades)}t {wr:.0f}%w\n", style="dim")
            p.append(f"avg +${aw:.2f} / -${abs(al):.2f}", style="dim")
        self._set("#pos", p)

        # base rates
        a = Text()
        wl = warnings()
        a.append("ALERTS ", style="bold")
        a.append(f"({len(wl)})\n" if wl else "\n", style="dim")
        if not wl:
            a.append("  nothing triggered -- quiet tape\n", style="dim")
        for sev, txt in wl[:4]:
            a.append("  \u2022 ", style=sev)
            a.append(f"{txt}\n", style=sev)
        bv, sv_, tn = vol_window(60)
        tot = bv + sv_
        if tot > 0:
            a.append("  60s volume  ", style="dim")
            a.append(f"buy {bv:.1f}", style="green")
            a.append(" / ", style="dim")
            a.append(f"sell {sv_:.1f}", style="red")
            a.append(f"   cvd {S.cvd:+.1f}\n", style="dim")
        self._set("#alerts", a)

        s = Text()
        if S.rates:
            r = S.rates
            s.append(f"BASE RATES  {S.regime}", style="bold")
            s.append(f"   n={r['n']:,} on 250ms capture\n", style="dim")
            s.append(f"  P(+20% in 60s) {r['r20']*100:>5.1f}%", style="white")
            s.append(f"   P(+50%) {r['r50']*100:>5.1f}%", style="white")
            s.append(f"   median {r['med']*100:>+5.1f}%", style="red" if r["med"] < 0 else "green")
            s.append(f"   mean {r['mean']*100:>+5.1f}%\n", style="dim")
            # the bot's own entry rules, evaluated now
            ch = gate_checks()
            npass = sum(1 for _, ok, _ in ch if ok)
            s.append("YOUR BOT'S GATES", style="bold")
            s.append(f"  {npass}/{len(ch)} pass\n", style="green" if npass == len(ch) else "yellow")
            for lbl, ok, val in ch:
                s.append(f"  {lbl} ", style="dim")
                s.append(f"{val:<5}", style="white")
                s.append("PASS " if ok else "FAIL ", style="green" if ok else "red")
            s.append("\n")
            s.append("Frequencies, not a recommendation. The full gate set "
                     "measured -6.0%/trade.", style="dim italic")
        else:
            s.append("warming up detector...", style="dim")
        self._set("#stats", s)

        # log
        lg = Text()
        if S.trades:
            for tr in S.trades[:6]:
                c = "green" if tr["pnl"] >= 0 else "red"
                lg.append(f"{tr['ts']} {tr['side'].upper():<4} "
                          f"{tr['entry']:.3f}->{tr['exit']:.3f} ", style="dim")
                lg.append(f"{tr['pct']*100:+6.1f}% ${tr['pnl']:+6.2f}\n", style=c)
        else:
            lg.append("no trades yet -- u to buy UP, d to buy DOWN", style="dim")
        self._set("#log", lg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="LIVE: orders are real. needs PK in env/.env")
    ap.add_argument("--dry", action="store_true",
                    help="checks credentials, tick size and price bound; "
                         "nothing is signed or sent")
    ap.add_argument("--stake", type=int, default=5)
    a = ap.parse_args()
    S.stake = a.stake

    if a.live or a.dry:
        import execution
        S.broker = execution.LiveBroker(dry_run=a.dry)
        S.live = bool(S.broker.ok)
        S.funder = S.broker.funder
        if not S.broker.ok:
            print(f"!! live requested but broker not ready: {S.broker.error}")
            print("!! starting in PAPER mode")
        else:
            S.balance = S.broker.balance()
            cap = f"${S.broker.max_usdc:.2f}" if S.broker.max_usdc > 0 else "no cap"
            print(f"{'DRY' if a.dry else 'LIVE'} signer={S.broker.addr} "
                  f"funder={S.broker.funder} max/order={cap}")
            if a.dry:
                print("DRY RUN: credentials checked; nothing is signed or sent")

    TraderApp().run()


if __name__ == "__main__":
    main()
