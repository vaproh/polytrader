"""Live taker tape: who just bought, mirrored from the website."""
from __future__ import annotations

import asyncio

from .. import poly
from ..state import S

_seen: set = set()
_ws = 0


def _reset(ws):
    """A new window gets a fresh tape."""
    global _ws
    if ws != _ws:
        _ws = ws
        _seen.clear()
        S.flow = []


async def _poll():
    """One tape refresh. Split out so tests can drive it without the loop."""
    try:
        ws = S.ws_start
        if not ws:
            return
        if not S.cond:
            # fresh start mid-window: the rollover hasn't provided the
            # event id yet, so fetch it directly instead of staying dark
            # until the next boundary.
            try:
                m = await asyncio.to_thread(
                    poly.market, f"btc-updown-5m-{ws}")
            except Exception:
                return
            if not m:
                return
            S.cond = m.get("condition_id", "")
            if not S.cond:
                return
        _reset(ws)
        for f in poly.recent_trades(S.cond):
            key = (f["ts"], f["side"], f["usd"])
            if key in _seen:
                continue
            _seen.add(key)
            S.flow.append(f)
        S.flow.sort(key=lambda f: f["ts"], reverse=True)
        del S.flow[15:]
        if len(_seen) > 500:
            _seen.clear()
    except Exception:
        pass


async def flow_loop():
    """Poll the event's recent fills every few seconds."""
    while True:
        await _poll()
        await asyncio.sleep(5.0)
