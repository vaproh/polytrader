"""Text builders for the panels."""
from __future__ import annotations

import time
from datetime import datetime

from rich.text import Text

from ..config import effective_preset, fmt_leg
from ..detector import (btc_state, fair_p, gate_checks, rsi_fast, rsi_last,
                        vol_window, warnings)
from ..state import S, cur_book, mark_price, secs_left

REG_STYLE = {"DEAD": "red", "TREND": "red", "CHOP": "green",
             "CHOP-DOWN": "bright_green", "VOLATILE": "yellow",
             "QUIET-UP": "dim"}


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


def clock(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _gap(px):
    gap = px - S.strike
    return gap, (gap / S.strike * 100 if S.strike else 0.0)


def _tick_arrow(cur, open_):
    """Cumulative move since the 1-minute candle opened.

    Adds every tick on top of the last, so the number stays and grows through
    the minute instead of flickering per paint. Blank until the first tick.
    """
    if not cur or not open_:
        return None
    d = cur - open_
    if d > 0:
        return ("green", f"▲ +{d:,.2f}")
    if d < 0:
        return ("red", f"▼ {d:,.2f}")
    return ("dim", "▬ ±0.00")


def _sign_style(v):
    return "green" if v >= 0 else "red"


def _usd(v):
    return f"${v:,.2f}"


def _c(v):
    """A 0-1 contract price in cents, the way the market quotes it."""
    return f"{v * 100:.1f}¢"


def twap_source():
    """Which feed produced the beat/TWAP, including a stale exact value."""
    if S.twap_src == "chainlink":
        return "chainlink" if time.time() - S.twap_ts < 10 else "chainlink stale"
    return S.twap_src or ""


def make_status_text():
    """One line: spot, beat, and the resolution TWAP vs the beat."""
    t = Text()
    t.append("BTC ", style="dim")
    t.append(f"${S.btc:,.2f}", style="bold white")
    if S.strike:
        t.append("   beat ", style="dim")
        t.append(f"${S.strike:,.2f}", style="white")
        src = twap_source()
        if src:
            t.append(f" ({src})", style="dim")
    if S.twap:
        gap, pct = _gap(S.twap)
        t.append("   TWAP ", style="dim")
        t.append(f"${S.twap:,.2f}", style="white")
        t.append(f" {gap:+,.2f} ({pct:+.2f}%)", style=_sign_style(gap))
        t.append(f" {'UP' if gap >= 0 else 'DOWN'}", style=f"bold {_sign_style(gap)}")
    if S.session_open:
        ch = (S.btc / S.session_open - 1) * 100
        t.append(f"   {ch:+.2f}% session", style=_sign_style(ch))
    mode = ("DRY" if (S.broker and getattr(S.broker, "dry_run", False))
            else ("LIVE" if S.live else "PAPER"))
    col = "yellow" if mode == "DRY" else ("red" if S.live else "cyan")
    t.append(f"   [{mode}]", style=f"bold {col}")
    if S.live and S.funder:
        t.append(f" {S.funder[:6]}..{S.funder[-4:]}", style="dim")
        if S.balance is not None:
            t.append(f" ${S.balance:.2f}", style="green")
    t.append(f"   stake {_usd(S.stake)}", style="yellow")
    if S.paused:
        t.append("   PAUSED", style="bold yellow")
    if time.time() < S.flash_until:
        t.append("   ● FILLED", style="bold yellow")
    if S.msg:
        t.append(f"   last: {S.msg}", style="dim")

    # cash line: account collateral and the order this stake would place
    t.append("\nCASH ", style="dim")
    order = _usd(S.stake) if S.stake > 0 else "-- (press a)"
    if S.live:
        t.append(_usd(S.balance) if S.balance is not None else "--",
                 style="bold green")
        t.append(f"   ORDER {order}", style="yellow")
        if S.balance is not None and S.stake > 0:
            left = S.balance - S.stake
            t.append(f"   AFTER {_usd(left)}", style="red" if left < 0 else "dim")
        cap = getattr(S.broker, "max_usdc", 0.0) if S.broker else 0.0
        t.append(f"   cap {_usd(cap)}/order" if cap > 0 else "   cap none",
                 style="dim")
    else:
        t.append("-- (paper)", style="dim")
        t.append(f"   ORDER {order}", style="yellow")
        t.append("   cap --", style="dim")
    return t


def make_window_text():
    """The resolution block: beat, current TWAP, and spot, all vs the beat."""
    t = Text()
    t.append("WINDOW  ", style="bold")
    t.append(S.slug or "waiting for the window", style="white")
    if S.ws_start:
        now = time.time()
        if now < S.ws_start:
            # rolled early: the displayed window opens in the future
            t.append(f"   opens in {clock(S.ws_start - now)}", style="yellow")
            done = 0.0
        else:
            left = secs_left()
            t.append(f"   closes in {clock(left)}",
                     style="yellow" if left < 30 else "white")
            done = min(1.0, max(0.0, (300 - left) / 300))
        t.append(f"  {bar(done)} {done * 100:.0f}%", style="dim")
    t.append("\n")

    t.append("PRICE TO BEAT   ", style="dim")
    t.append(f"${S.strike:,.2f}" if S.strike else "--", style="bold white")
    src = twap_source()
    if S.strike and src:
        t.append(f"  ({src})", style="dim")
    t.append("\n")

    t.append("TWAP NOW        ", style="dim")
    if S.twap:
        gap, pct = _gap(S.twap)
        t.append(f"${S.twap:,.2f}", style="white")
        t.append(f"   {gap:+,.2f}  {pct:+.2f}%", style=_sign_style(gap))
        t.append(f"   {'UP' if gap >= 0 else 'DOWN'}", style=f"bold {_sign_style(gap)}")
        topen = (S.twap_open if S.twap_open_bar == int(time.time() // 60)
                 and S.twap_src == "chainlink" else 0.0)
        arrow = _tick_arrow(S.twap, topen)
        if arrow:
            t.append(f"   {arrow[1]}", style=arrow[0])
    else:
        t.append("warming up (60s mean)", style="dim")
    t.append("\n")

    t.append("SPOT (binance)  ", style="dim")
    if S.btc and S.strike:
        gap, pct = _gap(S.btc)
        t.append(f"${S.btc:,.2f}", style="white")
        t.append(f"   {gap:+,.2f}  {pct:+.2f}%", style=_sign_style(gap))
        t.append("   above beat (not resolution)" if gap >= 0
                 else "   below beat (not resolution)", style="dim")
        arrow = _tick_arrow(S.btc, S.candle_open)
        if arrow:
            t.append(f"   {arrow[1]}", style=arrow[0])
    else:
        t.append("--", style="dim")
    t.append("\n")

    sp = btc_spark()
    if sp:
        t.append("TAPE 180s       ", style="dim")
        t.append(sp, style="cyan")
        t.append("\n")

    t.append("resolution: 60s TWAP at close vs beat at open. Up if final >= beat.",
             style="dim italic")
    return t


def make_books_text():
    b = Text()
    bu, bd = cur_book("up"), cur_book("dn")
    ub = bu.best_bid() if bu else None
    ua = bu.best_ask() if bu else None
    db = bd.best_bid() if bd else None
    da = bd.best_ask() if bd else None

    b.append("MARKET  ", style="bold")
    b.append("pair ", style="dim")
    b.append(f"bid {_c(ub + db)}" if ub is not None and db is not None else "bid --",
             style="yellow")
    b.append("   ", style="dim")
    b.append(f"ask {_c(ua + da)}" if ua is not None and da is not None else "ask --",
             style="yellow")
    if ua is not None and da is not None:
        b.append(f"   overround {(ua + da - 1) * 100:+.1f}¢", style="dim")
    if S.book_ts:
        age = time.time() - S.book_ts
        if age < 3:
            b.append("   fresh", style="green")
        else:
            b.append(f"   {age:.0f}s stale", style="bold yellow")
    b.append("\n")

    for side, label in (("up", "UP  "), ("dn", "DOWN")):
        bk = cur_book(side)
        if not bk or bk.best_bid() is None:
            b.append(f"{label}  (no book)\n", style="dim")
            continue
        bb, ba = bk.best_bid(), bk.best_ask()
        dbid, dask = bk.depth()
        b.append(f"{label}  ", style="bold white")
        b.append(f"bid {_c(bb)} x{bk.bid_size():>5.0f}" if bb is not None
                 else "bid  --", style="green")
        b.append(f"   ask {_c(ba)} x{bk.ask_size():>5.0f}" if ba is not None
                 else "   ask  --", style="red")
        b.append(f"   depth3c ${dbid * (bb or 0):>6.0f}", style="dim")
        b.append(f" / ${dask * (ba or 0):>5.0f}\n", style="dim")

    if ua is not None or da is not None:
        b.append("ORDER  ", style="bold")
        if S.stake > 0:
            b.append(_usd(S.stake), style="yellow")
            if ua is not None:
                b.append(f" -> UP {S.stake / ua:.1f}sh", style="dim")
            if da is not None:
                b.append(f" / DOWN {S.stake / da:.1f}sh", style="dim")
        else:
            b.append("-- (press a)", style="dim")
        b.append("\n")

    if S.ws_start:
        nxt = S.ws_start + 300
        left_n = max(0.0, nxt - time.time())
        b.append(f"NEXT btc-updown-5m-{nxt} starts in {clock(left_n)}\n",
                 style="bold")
        nbooks = []
        for side in ("up", "dn"):
            aid = S.next_tokens.get(side)
            nbooks.append(S.next_books.get(aid) if aid else None)
        if all(nbooks) and all(k.best_bid() is not None for k in nbooks):
            nbu, nbd = nbooks
            b.append(f"{_c(nbu.best_bid())}/{_c(nbu.best_ask())} UP   "
                     f"{_c(nbd.best_bid())}/{_c(nbd.best_ask())} DN",
                     style="dim")
        else:
            b.append("not listed yet", style="dim")

    return b


def make_signal_text():
    t = Text()
    reg = S.regime
    t.append("SIGNAL  ", style="bold")
    t.append(reg, style=f"bold {REG_STYLE.get(reg, 'white')}")
    t.append("\n")

    if reg == "WARMUP":
        t.append(f"  book history {S.warm * 100:3.0f}%  {bar(S.warm)}\n", style="yellow")
        t.append("  need ~31s before the detector is valid\n", style="dim")
        return t

    fp = fair_p()
    bu = cur_book("up")
    mk = bu.best_ask() if bu else None
    t.append("  fair P(up) ", style="dim")
    if fp is not None:
        t.append(f"{fp * 100:3.0f}%", style="bold white")
    else:
        t.append("--", style="dim")
        t.append(f" (needs 20 bars, {len(S.klines)}/20)", style="dim")
    if mk is not None:
        t.append("   market ", style="dim")
        t.append(f"{_c(mk)}", style="white")
        if fp is not None:
            gap = fp - mk
            t.append("   gap ", style="dim")
            t.append(f"{gap * 100:+.1f}¢", style="dim")
    t.append("\n")

    r = S.rates or {}
    if r:
        t.append(f"  token bid 60s  +20% {r['r20'] * 100:4.1f}%", style="white")
        t.append(f"   +50% {r['r50'] * 100:4.1f}%", style="white")
        t.append(f"   n={r['n']:,}\n", style="dim")
        t.append(f"  median {r['med'] * 100:+.1f}%", style="red" if r["med"] < 0 else "green")
        t.append(f"   mean {r['mean'] * 100:+.1f}%\n", style="dim")

    st = btc_state()
    if st is not None:
        t.append(f"  z {st['z']:+.2f}sd   vol {st['sig'] * 1e4:.2f}bp/s   "
                 f"travel ${st['travel']:.0f}\n", style="dim")
    else:
        t.append("  z --   vol --   travel --\n", style="dim")

    t.append("  30s ", style="dim")
    t.append(f"{st['m30']:+.2f}" if st else "--",
             style=_sign_style(st["m30"]) if st else "dim")
    t.append("   60s ", style="dim")
    t.append(f"{st['m60']:+.2f}" if st else "--",
             style=_sign_style(st["m60"]) if st else "dim")
    t.append("   RSI(1m) ", style="dim")
    warmed = [p for p in (6, 12, 14, 24) if len(S.klines) >= p + 1]
    if warmed:
        for p in warmed:
            r = rsi_last(S.klines, p)
            col = "red" if r >= 70 else ("green" if r <= 30 else "white")
            t.append(f"{p}:{r:.1f} ", style=col)
    else:
        t.append(f"-- ({len(S.klines)}/7 bars)", style="dim")
    rf = rsi_fast()
    if rf is None:
        t.append("  5s:--", style="dim")
    else:
        col = "red" if rf >= 70 else ("green" if rf <= 30 else "white")
        t.append(f"  5s:{rf:.1f}", style=col)
    t.append("\n")

    ch = gate_checks()
    npass = sum(1 for _, ok, _ in ch if ok)
    t.append(f"  gates {npass}/{len(ch)}  ", style="bold")
    for lbl, ok, val in ch:
        t.append(f"{lbl} {val} ", style="dim")
        t.append("PASS  " if ok else "FAIL  ", style="green" if ok else "red")
    t.append("\n")

    t.append("  model readout, not an edge: market Brier 0.08 vs model 0.18",
             style="dim italic")
    return t


def make_position_text():
    p = Text()
    p.append("POSITION  ", style="bold")
    if S.pos.side:
        mk = mark_price(S.pos.side)
        pnl = (mk - S.pos.entry) * S.pos.qty if mk is not None else 0.0
        pct = (mk / S.pos.entry - 1) * 100 if mk is not None and S.pos.entry else 0.0
        st = "green" if pnl >= 0 else "red"
        p.append(f"{S.pos.side.upper()}  {S.pos.qty:.1f} sh", style="bold")
        p.append(f"   cost {_usd(S.pos.entry * S.pos.qty)}", style="dim")
        if mk is not None:
            p.append(f"   value {_usd(mk * S.pos.qty)}", style="dim")
        p.append(f"   pays {_usd(S.pos.qty)}", style="green")
        p.append("\n")
        p.append("  entry ", style="dim")
        p.append(f"{_c(S.pos.entry)}", style="white")
        p.append("   mark ", style="dim")
        p.append(f"{_c(mk)}" if mk is not None else "--", style="white")
        p.append("   P&L ", style="dim")
        if mk is not None:
            p.append(f"${pnl:+.2f} ({pct:+.1f}%)\n", style=f"bold {st}")
        else:
            p.append("--\n", style="dim")

        pr = effective_preset(S.preset, S.risk)
        stop_px = (S.pos.entry if S.pos.locked
                   else _leg_price(S.pos.entry, S.pos.qty, pr["stop"], -1))
        p.append("  stop ", style="dim")
        if not S.stop_on:
            p.append("OFF", style="bold yellow")
        elif stop_px is None and not S.pos.locked:
            p.append("none (FREE)", style="dim")
        else:
            label = "BE " if S.pos.locked else ""
            p.append(f"{label}{_c(stop_px)}", style="red")
            if mk is not None and mk <= stop_px:
                p.append(" (breached)", style="bold red")
        tp_px = _leg_price(S.pos.entry, S.pos.qty, pr["tp"], +1)
        if not S.tp_on:
            p.append("   tp ", style="dim")
            p.append("OFF", style="bold yellow")
        elif tp_px is not None:
            p.append("   tp ", style="dim")
            p.append(f"{_c(tp_px)}" if tp_px <= 0.99 else "unreachable",
                     style="green" if tp_px <= 0.99 else "bold yellow")
        p.append(f"   held {time.time() - S.pos.ts:.0f}s\n", style="dim")
    else:
        p.append("FLAT\n", style="dim")

    tot = "green" if S.pnl_session >= 0 else "red"
    p.append(f"  session ${S.pnl_session:+.2f}", style=f"bold {tot}")
    if S.trades:
        wins = [t for t in S.trades if t["pnl"] > 0]
        losses = [t for t in S.trades if t["pnl"] <= 0]
        wr = len(wins) / len(S.trades) * 100
        aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
        al = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
        p.append(f"   {len(S.trades)}t {wr:.0f}%w\n", style="dim")
        p.append(f"  avg +${aw:.2f} / -${abs(al):.2f}\n", style="dim")
    else:
        p.append("\n", style="dim")

    pr = effective_preset(S.preset, S.risk)
    stop, tp = pr["stop"], pr["tp"]
    if not S.stop_on:
        stop_state = "OFF"
    elif _leg_on(stop):
        stop_state = "ARMED"
    else:
        stop_state = "none"
    p.append(f"  preset {S.preset}   stop {stop_state}", style="dim")
    if S.stop_on and _leg_on(stop):
        p.append(f" ({fmt_leg(stop, '-')})", style="dim")
    p.append(f"   tp {'OFF' if not S.tp_on else fmt_leg(tp)}"
             f"   be +{pr['be'] * 100:.0f}%\n", style="dim")
    return p


def _leg_on(leg):
    return leg["v"] > 0


def _leg_price(entry, qty, leg, sign):
    """The contract price where a stop (-1) or TP (+1) leg fires.

    Dollar legs divide by the position size; without a size there is no
    price, so they read as unset. A zero leg is disabled and reads as None.
    """
    if leg["v"] <= 0:
        return None
    if leg["by"] == "usd":
        return entry + sign * leg["v"] / qty if qty else None
    return entry * (1 + sign * leg["v"])


def make_alerts_text():
    a = Text()
    wl = warnings()
    a.append(f"ALERTS ({len(wl)})", style="bold")
    if not wl:
        a.append("   nothing triggered -- quiet tape", style="dim")
    bv, sv, _ = vol_window(60)
    if bv + sv > 0:
        a.append("   60s vol ", style="dim")
        a.append(f"buy {bv:.1f}", style="green")
        a.append(" / ", style="dim")
        a.append(f"sell {sv:.1f}", style="red")
        a.append(f"   CVD session {S.cvd:+.1f}", style="dim")
    a.append("\n")
    for sev, txt in wl[:3]:
        a.append("  \u2022 ", style=sev)
        a.append(f"{txt}\n", style=sev)
    return a


def _trade_row(t, tr):
    c = "green" if tr["pnl"] >= 0 else "red"
    t.append(f"{tr['ts']}  ", style="dim")
    t.append(f"{tr['side'].upper():<4} ", style="bold")
    t.append(f"{tr.get('mode', ''):<1}  ", style="dim")
    t.append(f"{_c(tr['entry'])} -> {_c(tr['exit'])}  ", style="dim")
    t.append(f"{tr['pct'] * 100:+6.1f}%  ", style=c)
    t.append(f"${tr['pnl']:+6.2f}", style=c)
    reason = tr.get("reason", "")
    if reason:
        t.append(f"  {reason}", style="dim")
    t.append("\n")


def _day_label(day):
    """'2026-10-26' -> 'Mon Oct 26'. Unparseable days pass through."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%a %b %d")
    except Exception:
        return day


def make_trades_text():
    t = Text()
    if S.history_mode:
        rows = list(S.trades) + list(reversed(S.history))
        pnl = S.pnl_session + sum(h.get("pnl", 0.0) for h in S.history)
        t.append(f"ALL-TIME {len(rows)} trades  P&L ${pnl:+.2f}",
                 style="bold green" if pnl >= 0 else "bold red")
        t.append("   h session · J/K scroll\n", style="dim")
        groups: dict[str, list] = {}
        for tr in rows:
            groups.setdefault(tr.get("day") or "earlier", []).append(tr)
        days = sorted((d for d in groups if d != "earlier"), reverse=True)
        if "earlier" in groups:
            days.append("earlier")  # rows stamped before days existed
        shown = 0
        for day in days:
            if shown >= 100:
                break
            drows = groups[day]
            dpnl = sum(r.get("pnl", 0.0) for r in drows)
            label = _day_label(day) if day != "earlier" else "earlier"
            t.append(f"{label} · {len(drows)} trades · ${dpnl:+.2f}\n",
                     style="bold")
            for tr in drows:
                if shown >= 100:
                    break
                _trade_row(t, tr)
                shown += 1
        if len(rows) > shown:
            t.append(f"   ... {len(rows) - shown} older (file keeps 2000)",
                     style="dim")
        return t
    if not S.trades:
        t.append("no trades yet -- u buy UP, d buy DOWN", style="dim")
        t.append("\n   h all-time history", style="dim")
        return t
    t.append(f"SESSION {_day_label(time.strftime('%Y-%m-%d'))} · "
             f"{len(S.trades)} trades  P&L ${S.pnl_session:+.2f}",
             style="bold green" if S.pnl_session >= 0 else "bold red")
    t.append("   h all-time · J/K scroll\n", style="dim")
    for tr in S.trades[:12]:
        _trade_row(t, tr)
    return t
