"""Money rules: exits, settlement, and the 5-minute rollover."""
from __future__ import annotations

import time

from .config import PRESETS
from .detector import twap60
from .state import S, mark_price


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
    # Resolution compares 60s Chainlink TWAPs, so the opening reference is the
    # 60s mean ending at the boundary, converted from USDT to USD. Spot is the
    # fallback when the feed has not been up long enough to average.
    tx = twap60(now=ws, min_samples=30)
    # both branches must land in USD: the live TWAP is also multiplied by the rate
    S.strike = (tx or S.btc or (S.klines[-1] if S.klines else 0.0)) * S.usdt_usd
    S.twap = S.strike
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
