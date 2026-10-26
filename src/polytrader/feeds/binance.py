"""Binance BTC/USDT trades and the 1-minute bar series."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from ..state import S

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


def fetch_klines(limit=120):
    """Seed 1m closes from Binance so RSI(14) is valid immediately."""
    try:
        u = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             f"&interval=1m&limit={limit}")
        raw = urllib.request.urlopen(u, timeout=12).read()
        return [float(r[4]) for r in json.loads(raw.decode())]
    except Exception:
        return []


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
