"""5-minute window rollover and token prefetch."""
from __future__ import annotations

import asyncio
import time

from .. import poly
from ..engine import roll_to_window
from ..state import S, mark_price


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
                    S.msg += note
            except Exception as e:
                S.msg = f"window: {str(e)[:40]}"
            S.window_loading = False

        # wake exactly on the boundary instead of up to 1s late
        now = time.time()
        nxt = (int(now // 300) + 1) * 300
        await asyncio.sleep(max(0.05, min(1.0, nxt - now + 0.01)))
