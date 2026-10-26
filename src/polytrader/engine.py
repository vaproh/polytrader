"""Money rules: exits, settlement, and the 5-minute rollover."""
from __future__ import annotations

import json
import time

from . import config
from .config import effective_preset, fmt_leg
from .detector import twap60
from .state import S, mark_price


def record_trade(tr, path=None):
    """Append one closed trade to the local history file. Never raises."""
    try:
        with open(path or config.HISTORY_PATH, "a") as fh:
            fh.write(json.dumps(tr) + "\n")
    except Exception:
        pass


def load_history(limit=2000, path=None):
    """Past runs' trades, oldest first. Corrupt lines are skipped; the file
    is trimmed to `limit` rows so it cannot grow without bound."""
    p = path or config.HISTORY_PATH
    rows = []
    try:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    rows = [r for r in rows if isinstance(r, dict)]
    if len(rows) > limit:
        rows = rows[-limit:]
        try:
            with open(p, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
        except Exception:
            pass
    return rows


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
    tr = {"side": S.pos.side, "entry": S.pos.entry, "exit": px,
          "qty": q, "pnl": pnl,
          "pct": (px / S.pos.entry - 1) if S.pos.entry else 0.0,
          "ts": time.strftime("%I:%M:%S %p").lstrip("0"), "reason": reason,
          "mode": "L" if S.live else "P"}
    S.trades.insert(0, tr)
    del S.trades[200:]
    record_trade(tr)
    S.pos.qty -= q
    # Orders round shares to 2dp, so a remainder below half a hundredth can
    # never be sent: the SDK refuses it as "shares must be a positive number".
    # Absorb the dust instead of leaving a 0.0-share position that blocks the
    # next buy and fails every sell.
    if S.pos.qty <= 1e-9 or round(S.pos.qty, 2) == 0:
        S.pos.clear()
        S.msg = f"closed {reason}"
    else:
        S.msg = f"partial {reason}: {q:.1f}sh @ {px:.3f}"
    S.flash_until = time.time() + 2.0
    S.bell_pending = True


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
    # Resolution compares 60s Chainlink TWAPs. The exact public feed overwrites
    # this a moment later; the proxy is the fallback when that feed is down.
    tx = twap60(now=ws, min_samples=30)
    # both branches must land in USD: the live TWAP is also multiplied by the rate
    S.strike = (tx or S.btc or (S.klines[-1] if S.klines else 0.0)) * S.usdt_usd
    S.twap = S.strike
    S.twap_src = "proxy"
    S.twap_ts = 0.0
    S.twap_data_ts = 0.0
    S.mid_hist.clear()
    S.books.clear()
    S.msg = f"window {ws}"


def exit_decision(entry, mark, preset, stop_on=True, locked=False, qty=None):
    """Pure exit rule: (reason or None, locked).

    Order matters: breakeven arms first, then TP, then the stop. If TP and the
    stop could both fire on one tick, TP wins -- you were in profit. Dollar
    legs need the position size to evaluate, so without `qty` they stay inert.
    """
    pr = effective_preset(preset, S.risk)
    if pr["be"] > 0 and not locked and mark >= entry * (1 + pr["be"]):
        locked = True
    tp, stop = pr["tp"], pr["stop"]
    if _tp_hit(entry, mark, qty, tp):
        return f"TAKE PROFIT {fmt_leg(tp)}", locked
    if not stop_on:
        return None, locked
    if locked:
        if mark <= entry:
            return "BE STOP", locked
        return None, locked
    if _stop_hit(entry, mark, qty, stop):
        return f"STOP {fmt_leg(stop, '-')}", locked
    return None, locked


def _tp_hit(entry, mark, qty, leg):
    if leg["by"] == "usd":
        return (bool(qty) and leg["v"] > 0
                and (mark - entry) * qty >= leg["v"])
    return leg["v"] > 0 and mark >= entry * (1 + leg["v"])


def _stop_hit(entry, mark, qty, leg):
    if leg["by"] == "usd":
        return (bool(qty) and leg["v"] > 0
                and (mark - entry) * qty <= -leg["v"])
    return leg["v"] > 0 and mark <= entry * (1 - leg["v"])
