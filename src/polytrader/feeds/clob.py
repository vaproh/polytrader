"""Polymarket CLOB market channel and its REST fallback."""
from __future__ import annotations

import asyncio
import json
import time

from .. import poly
from ..state import S, Book

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


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
                b = await asyncio.to_thread(poly.book, aid)
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
