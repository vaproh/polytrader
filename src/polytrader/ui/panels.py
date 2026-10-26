"""Text builders for the panels."""
from __future__ import annotations

from rich.text import Text

from ..state import S, cur_book, secs_left


def bar(v, width=12):
    v = max(0.0, min(1.0, v))
    n = int(round(v * width))
    return "█" * n + "·" * (width - n)


def btc_spark(width=44, lookback=180.0):
    """ASCII sparkline of recent BTC ticks, for context only."""
    if len(S.btc_hist) < 4:
        return ""
    now = S.btc_hist[-1][0]
    pts = [p for t, p in S.btc_hist if t >= now - lookback]
    if len(pts) < 4:
        return ""
    step = max(1, len(pts) // width)
    pts = pts[::step][-width:]
    # 3-point smoothing: raw last-trade prices bounce between bid and ask and
    # an unsmoothed spark looks like a square wave rather than a price path
    if len(pts) >= 5:
        sm = []
        for i in range(len(pts)):
            lo_i, hi_i = max(0, i - 1), min(len(pts), i + 2)
            sm.append(sum(pts[lo_i:hi_i]) / (hi_i - lo_i))
        pts = sm
    lo, hi = min(pts), max(pts)
    if hi - lo < 1e-9:
        return "\u2500" * len(pts)
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    return "".join(blocks[min(7, int((p - lo) / (hi - lo) * 7.999))] for p in pts)


def make_books_text():
    b = Text()
    b.append(f"{S.slug}", style="bold")
    b.append(f"   resolves in {secs_left():.0f}s\n\n")
    for side, label in (("up", "UP  "), ("dn", "DOWN")):
        bk = cur_book(side)
        if not bk or bk.best_bid() is None:
            b.append(f"{label}  (no book)\n", style="dim")
            continue
        bb, ba = bk.best_bid(), bk.best_ask()
        db, da = bk.depth()
        b.append(f"{label}  ", style="bold white")
        b.append(f"bid {bb:.2f}" if bb is not None else "bid  --", style="green")
        b.append(f"   ask {ba:.2f}" if ba is not None else "   ask  --", style="red")
        b.append(f"   q {bk.bid_size():>5.0f}/{bk.ask_size():>5.0f}", style="white")
        b.append(f"   depth3c ${db*(bb or 0):>6.0f}", style="dim")
        b.append(f"   size@ask ${(ba or 0)*bk.ask_size():>6.0f}\n", style="dim")
    bu, bd = cur_book("up"), cur_book("dn")
    if bu and bd and bu.best_bid() and bd.best_bid():
        ps = bu.best_bid() + bd.best_bid()
        b.append(f"\npair sum {ps:.4f}", style="yellow")
        b.append(f"   you pay {abs(1-ps)*100:.2f}c/contract vs 1.0000",
                 style="dim")

    return b
