"""USDT/USD basis: Binance quotes USDT, the market resolves on USD.

Measured against Polymarket's published Chainlink TWAP, the 60s Binance mean
times this rate lands within a few dollars; unadjusted it is off by the basis.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from ..state import S

KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=USDTZUSD"


def fetch_usdt_usd(timeout=3.0):
    """Latest USDT/USD trade from Kraken, or None. Never raises."""
    try:
        req = urllib.request.Request(KRAKEN, headers={"User-Agent": "polytrader"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        pair = next(iter(data["result"].values()))
        val = float(pair["c"][0])
        return val if 0.9 < val < 1.1 else None
    except Exception:
        return None


async def basis_loop():
    """Refresh the rate once a minute; a failed fetch keeps the last value."""
    while True:
        rate = await asyncio.to_thread(fetch_usdt_usd)
        if rate:
            S.usdt_usd = rate
            S.usdt_usd_ts = time.time()
        await asyncio.sleep(60)
