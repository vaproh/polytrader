"""5-minute window rollover and token prefetch."""
from __future__ import annotations

import asyncio
import time

from .. import poly
from ..engine import roll_to_window
from ..state import S, Book, mark_price


def _parse_tokens(m):
    toks = dict(zip([o.lower() for o in m["outcomes"]], m["token_ids"]))
    return {"up" if k in ("up", "yes") else "dn": v for k, v in toks.items()}


async def _prefetch(ws_next):
    """Warm the token cache for the NEXT window before it starts."""
    if ws_next in S.prefetch or ws_next in S.prefetch_inflight:
        return
    S.prefetch_inflight.add(ws_next)
    try:
        m = await asyncio.to_thread(poly.market, f"btc-updown-5m-{ws_next}")
        if m:
            S.prefetch[ws_next] = _parse_tokens(m)
    except Exception:
        pass
    finally:
        S.prefetch_inflight.discard(ws_next)


async def _refresh_next(ws_next):
    """Pull the next window's tokens and books for the preview and pre-entry.

    The market usually lists about a minute early. Never raises, never touches
    the current window.
    """
    if S.ws_start and ws_next != S.ws_start + 300:
        return
    try:
        toks = S.prefetch.get(ws_next)
        if toks is None:
            m = await asyncio.to_thread(poly.market, f"btc-updown-5m-{ws_next}")
            if not m:
                return
            toks = _parse_tokens(m)
            S.prefetch[ws_next] = toks
        if "up" not in toks or "dn" not in toks:
            return
        books = {}
        for aid in toks.values():
            b = await asyncio.to_thread(poly.book, aid)
            if b and b.get("best_bid") is not None:
                bk = Book()
                bk.bids = {b["best_bid"]: b.get("bid_size") or 1.0}
                if b.get("best_ask") is not None:
                    bk.asks = {b["best_ask"]: b.get("ask_size") or 1.0}
                books[aid] = bk
        if not books:
            return
        if S.ws_start and ws_next != S.ws_start + 300:
            return  # rolled while fetching; belongs to a gone window
        S.next_tokens = toks
        S.next_books = books
        S.next_slug = f"btc-updown-5m-{ws_next}"
        S.next_ws = ws_next
        S.next_ts = time.time()
    except Exception:
        pass


def _absorb_next(ws):
    """The previewed books are now the current books."""
    if S.next_ws == ws:
        S.books.update(S.next_books)
        S.book_dirty = True
        S.next_tokens = {}
        S.next_books = {}
        S.next_slug = ""
        S.next_ws = 0
        S.next_ts = 0.0


def _early_roll(ws):
    """Roll into the next window up to 60s early, once its book is live.

    Flat only: holding a current-window position keeps the old window until
    the boundary settles it. Returns True when it rolled.
    """
    if S.pos.side or S.window_loading:
        return False
    nxt = ws + 300
    if S.ws_start != ws or S.next_ws != nxt or not S.next_books:
        return False
    for side in ("up", "dn"):
        aid = S.next_tokens.get(side)
        bk = S.next_books.get(aid) if aid else None
        if not bk or bk.best_bid() is None:
            return False
    roll_to_window(nxt, dict(S.next_tokens))
    _absorb_next(nxt)
    S.msg = f"early window {nxt}"
    return True


async def window_loop():
    """Roll to the current 5-min window. Prefetches so the switch is instant."""
    while True:
        now = time.time()
        ws = int(now // 300 * 300)
        left = 300 - (now - ws)

        # warm the next window a minute ahead so rollover costs no round-trip
        if left <= 60:
            asyncio.create_task(_prefetch(ws + 300))
        # ...and keep its books warm for the preview and the early switch
        if left <= 90 and now - S.next_ts > 10:
            asyncio.create_task(_refresh_next(ws + 300))
        # under a minute left with a live next book: trade the next window
        if left <= 60 and _early_roll(ws):
            pass  # msg set inside; the rollover block below stands down

        if not S.window_loading and ws > S.ws_start:
            S.window_loading = True
            try:
                toks = S.prefetch.pop(ws, None)
                if toks is None:
                    m = await asyncio.to_thread(poly.market, f"btc-updown-5m-{ws}")
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
                    _absorb_next(ws)  # carry over the previewed books if any
                    S.msg += note
            except Exception as e:
                S.msg = f"window: {str(e)[:40]}"
            S.window_loading = False

        # wake exactly on the boundary instead of up to 1s late
        now = time.time()
        nxt = (int(now // 300) + 1) * 300
        await asyncio.sleep(max(0.05, min(1.0, nxt - now + 0.01)))
