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


def record(ts, side, usd, px, ws=None):
    """Append one fill to the tape. Socket and REST rows share
    second-granularity keys so the same fill arriving both ways shows once."""
    _reset(ws if ws is not None else S.ws_start)
    key = (int(ts), side, round(usd, 2))
    if key in _seen:
        return False
    _seen.add(key)
    S.flow.append({"ts": ts, "side": side, "usd": usd, "px": px})
    S.flow.sort(key=lambda f: f["ts"], reverse=True)
    del S.flow[15:]
    if len(_seen) > 500:
        _seen.clear()
    return True


def record_ws(m, tokens):
    """One socket last_trade_price message onto the tape. Never raises."""
    try:
        aid = m.get("asset_id")
        side = {v: k for k, v in tokens.items()}.get(aid) if aid else None
        if side not in ("up", "dn"):
            return False
        px = float(m["price"])
        size = float(m["size"])
        ts = float(m["timestamp"]) / 1000.0
        return record(ts, side, size * px, px)
    except Exception:
        return False


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
            record(f["ts"], f["side"], f["usd"], f["px"], ws)
    except Exception:
        pass


async def flow_loop():
    """Poll the event's recent fills every few seconds.

    Backfill only: live fills land instantly off the market socket.
    The poll catches anything the socket missed while it reconnects."""
    while True:
        await _poll()
        await asyncio.sleep(5.0)
