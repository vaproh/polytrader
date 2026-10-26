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
        if not ws or not S.cond:
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
