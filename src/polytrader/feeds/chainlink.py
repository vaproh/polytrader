"""Exact Chainlink BTC/USD TWAP from Polymarket's public window feed.

Polymarket's own 5m page reads `/api/crypto/price-history` for the TWAP chart
and the opening print. The endpoint is public, so the terminal can use the same
numbers instead of approximating them from Binance. The Binance proxy remains
the fallback when this feed is unreachable.

The first point of the history is the price to beat; the last point is the
current 60s TWAP. A completed window returns the final print too.
"""
from __future__ import annotations

import asyncio
import datetime
import http.client
import json
import threading
import time
import urllib.parse

from ..state import S

API = "https://polymarket.com/api/crypto/price-history"
OPEN_API = "https://polymarket.com/api/crypto/crypto-price"
HOST = "polymarket.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_conn: http.client.HTTPSConnection | None = None
_conn_lock = threading.Lock()


def _get(url, timeout=5.0):
    """GET JSON over one persistent connection; reconnects on failure.

    A fresh TLS handshake to polymarket.com costs ~1s from here, so a new
    connection per poll would add a handshake to every TWAP update. The
    connection is shared and recreated only when the server drops it.
    """
    global _conn
    path = url.split(HOST, 1)[1]
    headers = {"User-Agent": UA, "Accept": "application/json"}
    with _conn_lock:
        for attempt in (0, 1):
            try:
                if _conn is None:
                    _conn = http.client.HTTPSConnection(HOST, timeout=timeout)
                _conn.request("GET", path, headers=headers)
                resp = _conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    raise OSError(f"http {resp.status}")
                return json.loads(body.decode())
            except Exception:
                try:
                    if _conn is not None:
                        _conn.close()
                except Exception:
                    pass
                _conn = None
                if attempt == 1:
                    raise


def _iso(ts):
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_open(ws, timeout=5.0):
    """The exact opening TWAP for the window, or None. Never raises."""
    q = urllib.parse.urlencode({
        "symbol": "BTC",
        "eventStartTime": _iso(ws),
        "endDate": _iso(ws + 300),
        "variant": "fiveminute",
        "twapEnabled": "true",
        "twapLookbackSeconds": "60",
    })
    try:
        d = _get(f"{OPEN_API}?{q}", timeout)
        px = float(d.get("openPrice"))
        return px if px > 0 else None
    except Exception:
        return None


def fetch(ws, timeout=5.0):
    """Return {beat, now, ts, n}, or None. Never raises."""
    q = urllib.parse.urlencode({
        "symbol": "BTC",
        "eventStartTime": _iso(ws),
        "endDate": _iso(ws + 300),
        "twapEnabled": "true",
        "historySeed": "true",
        "twapLookbackSeconds": "60",
        "variant": "fiveminute",
    })
    try:
        rows = _get(f"{API}?{q}", timeout)
    except Exception:
        return None
    pts = []
    for row in rows if isinstance(rows, list) else []:
        try:
            ts = int(row["timestamp"]) / 1000.0
            px = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if ws <= ts <= ws + 300 and px > 0:
            pts.append((ts, px))
    if not pts:
        return None
    pts.sort()
    beat = pts[0][1]
    # a history starting late is still useful, but the exact open comes from
    # the sibling endpoint that reports openPrice directly
    if pts[0][0] > ws + 2:
        exact = fetch_open(ws, timeout)
        if exact:
            beat = exact
    return {"beat": beat, "now": pts[-1][1], "ts": pts[-1][0], "n": len(pts)}


def apply(ws, data):
    """Publish a fetch result, but only if it belongs to the current window."""
    if not data or S.ws_start != ws:
        return False
    S.strike = data["beat"]
    S.twap = data["now"]
    S.twap_ts = time.time()
    S.twap_src = "chainlink"
    return True


async def chainlink_loop():
    """Refresh the exact TWAP every second; the proxy covers failures."""
    while True:
        ws = S.ws_start
        if ws:
            data = await asyncio.to_thread(fetch, ws)
            apply(ws, data)
        await asyncio.sleep(1.0)
