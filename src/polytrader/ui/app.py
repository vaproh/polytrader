"""The Textual application: keys, timers, and the six panels."""
from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static

from ..config import PRESETS, save_cfg
from ..detector import (btc_state, fair_p, gate_checks, recompute_detector,
                        rsi_last, twap60, vol_window, warnings)
from ..engine import exit_decision, settle_position
from ..feeds.basis import basis_loop
from ..feeds.binance import binance_loop, fetch_klines
from ..feeds.clob import clob_loop, rest_fallback_loop
from ..feeds.window import window_loop
from ..state import S, cur_book, mark_price, secs_left
from .panels import bar, btc_spark, make_books_text


class Panel(Static):
    pass


class TraderApp(App):
    CSS = """
    Screen { background: #000000; }
    #top { height: 5; }
    #books { height: 8; }
    #duo { height: 10; }
    #alerts { height: 5; }
    #stats { height: 7; }
    #log { height: 1fr; }
    #regime { width: 1fr; }
    #pos { width: 1fr; }
    .box { border: round #444444; padding: 0 1; }
    """
    BINDINGS = [
        ("k", "toggle_stop", "stop"),
        ("]", "preset_next", "preset+"),
        ("[", "preset_prev", "preset-"),
        ("u", "buy('up')", "buy UP"),
        ("d", "buy('dn')", "buy DOWN"),
        ("s", "sell", "sell"),
        ("x", "flatten", "flatten"),
        ("1", "stake(1)"), ("2", "stake(2)"), ("3", "stake(3)"),
        ("4", "stake(4)"), ("5", "stake(5)"), ("6", "stake(6)"),
        ("7", "stake(7)"), ("8", "stake(8)"), ("9", "stake(9)"),
        ("o", "stake_cycle", "size up"),
        ("p", "pause", "pause"),
        ("r", "reload", "reload"),
        ("L", "toggle_live", "paper/live"),
        ("q", "quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Panel(id="top", classes="box")
        yield Panel(id="books", classes="box")
        with Horizontal(id="duo"):
            yield Panel(id="regime", classes="box")
            yield Panel(id="pos", classes="box")
        yield Panel(id="alerts", classes="box")
        yield Panel(id="stats", classes="box")
        yield Panel(id="log", classes="box")
        yield Footer()

    def on_mount(self):
        asyncio.create_task(self._seed_klines())
        self.set_interval(0.10, self.refresh_view)      # paint: 10 FPS
        self.set_interval(0.033, self.tick_view)        # 30 Hz: detect + paint
        asyncio.create_task(self._boot())

    async def _boot(self):
        asyncio.create_task(binance_loop())
        asyncio.create_task(window_loop())
        asyncio.create_task(rest_fallback_loop())
        asyncio.create_task(basis_loop())
        await asyncio.sleep(1.5)
        asyncio.create_task(clob_loop())

    # ---- keys
    def action_stake(self, n):
        S.stake = n
        save_cfg({"preset": S.preset, "stake": n})
        S.msg = f"stake ${n}"

    def action_stake_cycle(self):
        S.stake = 10 if S.stake >= 9 else S.stake + 1
        save_cfg({"preset": S.preset, "stake": S.stake})
        S.msg = f"stake ${S.stake}"

    def action_pause(self):
        S.paused = not S.paused
        S.msg = "paused" if S.paused else "running"

    def action_reload(self):
        S.ws_start = 0
        S.msg = "reloading window"

    def action_toggle_live(self):
        if not S.live and (not S.broker or not S.broker.ok):
            S.msg = "no live broker -- staying paper"
            return
        S.live = not S.live
        S.msg = ("LIVE -- orders will be real" if S.live
                 else "paper mode")

    def action_flatten(self):
        if S.live and S.pos.side and S.broker and S.broker.ok:
            bid = mark_price(S.pos.side) or 0.0
            S.busy = True
            S.msg = "FLATTENING"
            asyncio.create_task(self._live_sell(bid))
            self.refresh_view()
            return
        settle_position("flatten")
        self.refresh_view()

    def _set_preset(self, name):
        S.preset = name
        save_cfg({"preset": name, "stake": S.stake})
        pr = PRESETS[name]
        S.msg = (f"preset {name}: stop {pr['stop']*100:.0f}%  "
                 f"tp {pr['tp']*100:.0f}%  be {pr['be']*100:.0f}%")
        self.refresh_view()

    def action_preset_next(self):
        names = list(PRESETS)
        i = names.index(S.preset) if S.preset in names else -1
        self._set_preset(names[(i + 1) % len(names)])

    def action_preset_prev(self):
        names = list(PRESETS)
        i = names.index(S.preset) if S.preset in names else 0
        self._set_preset(names[(i - 1) % len(names)])

    def action_toggle_stop(self):
        S.stop_on = not S.stop_on
        S.msg = f"stop {'ARMED' if S.stop_on else 'OFF'}"
        self.refresh_view()

    def action_buy(self, side):
        if S.pos.side:
            S.msg = f"already {S.pos.side.upper()} -- s to sell first"
            return
        bk = cur_book(side)
        if not bk or not bk.best_ask():
            S.msg = "no ask on that side"
            return
        ask = bk.best_ask()
        if ask <= 0.01 or ask >= 0.99:
            S.msg = f"ask {ask:.2f} outside tradeable band"
            return
        if secs_left() < 20:
            S.msg = "too close to resolution"
            return
        qty = S.stake / ask
        if S.live:
            if not S.broker or not S.broker.ok:
                S.msg = "live order blocked: " + (S.broker.error if S.broker else "no broker")
                return
            if S.busy:
                S.msg = "order in flight -- wait"
                return
            # Do NOT block the render loop on signing + POST. Fire it in the
            # background and paint "SENDING" immediately.
            S.busy = True
            S.msg = f"SENDING {side.upper()} ${S.stake} @ {ask:.3f}"
            asyncio.create_task(self._live_buy(side, ask, qty))
        else:
            S.msg = (f"PAPER BUY {side.upper()} {qty:.1f}sh @ {ask:.3f} (${S.stake})")
            S.pos.side = side
            S.pos.qty = qty
            S.pos.entry = ask
            S.pos.ts = time.time()
            S.pos.stake = S.stake
        self.refresh_view()

    async def _live_buy(self, side, ask, qty):
        tok = S.tokens.get(side)
        try:
            ok, detail = await asyncio.to_thread(S.broker.buy, tok, S.stake, ask)
        except Exception as e:
            ok, detail = False, f"buy error: {str(e)[:60]}"
        S.busy = False
        if not ok:
            S.msg = detail
            return
        S.msg = detail
        filled = getattr(S.broker, "last_fill", 0.0) or qty
        price = getattr(S.broker, "last_price", 0.0) or ask
        S.pos.side = side
        S.pos.qty = filled
        S.pos.entry = price
        S.pos.ts = time.time()
        S.pos.stake = S.stake
        await self._refresh_balance()

    async def _live_sell(self, bid):
        side, qty = S.pos.side, S.pos.qty
        tok = S.tokens.get(side) if side else None
        try:
            ok, detail = await asyncio.to_thread(S.broker.sell, tok, qty, bid)
        except Exception as e:
            ok, detail = False, f"sell error: {str(e)[:60]}"
        S.busy = False
        if not ok:
            S.msg = detail
            return
        S.msg = detail
        px = getattr(S.broker, "last_price", 0.0) or bid
        sold = getattr(S.broker, "last_fill", 0.0) or qty
        settle_position("sold", px, sold)
        await self._refresh_balance()

    async def _refresh_balance(self):
        if not (S.broker and S.broker.ok):
            return
        try:
            bal = await asyncio.to_thread(S.broker.balance)
        except Exception:
            return
        if bal is not None:
            S.balance = bal

    def action_sell(self):
        if not S.pos.side:
            S.msg = "no position"
            return
        if S.live:
            if not S.broker or not S.broker.ok:
                S.msg = "live sell blocked: broker not ready"
                return
            if S.busy:
                S.msg = "order in flight -- wait"
                return
            bid = mark_price(S.pos.side) or 0.0
            S.busy = True
            S.msg = f"SENDING SELL {S.pos.qty:.1f}sh @ {bid:.3f}"
            asyncio.create_task(self._live_sell(bid))
            self.refresh_view()
            return
        settle_position("sold")
        self.refresh_view()

    # ---- render
    _sig = {}

    def _set(self, sel, text):
        """Push to a panel only if its rendered content actually changed."""
        if isinstance(text, Text):
            sig = (text.plain, tuple((sp.start, sp.end, str(sp.style)) for sp in text.spans))
        else:
            sig = str(text)
        if self._sig.get(sel) == sig:
            return False
        self._sig[sel] = sig
        try:
            self.query_one(sel, Panel).update(text)
        except Exception:
            # the screen is being torn down (quit / resize); drop the paint
            return False
        return True

    def paint_books(self):
        """Repaint just the book panel when new data lands (<=25/s)."""
        if not self.is_mounted or not S.book_dirty:
            return
        S.book_dirty = False
        self._set("#books", make_books_text())

    def _close_at(self, reason, mk):
        """Close at the current mark, live or paper."""
        if S.live and S.broker and S.broker.ok:
            now = time.time()
            # a rejected sell must not retry at 30 Hz; wait two seconds
            if S.busy or now < S.exit_retry_ts:
                return
            S.exit_retry_ts = now + 2.0
            S.busy = True
            S.msg = f"{reason} -- selling at {mk:.3f}"
            asyncio.create_task(self._live_sell(mk))
        else:
            settle_position(reason)
            self.refresh_view()

    async def _seed_klines(self):
        """Pull 1m candles once so RSI(14) is valid from the first frame."""
        S.klines = await asyncio.to_thread(fetch_klines)

    def check_exits(self):
        """Take profit, breakeven lock, hard stop. Evaluated at 30 Hz."""
        if not S.pos.side or not S.pos.entry:
            return
        mk = mark_price(S.pos.side)
        if mk is None:
            return
        reason, S.pos.locked = exit_decision(
            S.pos.entry, mk, S.preset, stop_on=S.stop_on, locked=S.pos.locked)
        if reason:
            self._close_at(reason, mk)

    def tick_view(self):
        """30 Hz: recompute the detector from fresh ticks, paint, enforce stop."""
        if not self.is_mounted:
            return
        if not S.paused:
            recompute_detector()
        self.paint_books()
        self.check_exits()

    def refresh_view(self):
        if not self.is_mounted:
            return
        if not S.paused:
            recompute_detector()
            tx = twap60(min_samples=30)
            S.twap = (tx * S.usdt_usd) if tx else 0.0

        # top
        btc = S.btc
        chg = ""
        if S.session_open:
            chg = f"{(btc/S.session_open-1)*100:+.2f}% session"
        mode = ("DRY" if (S.broker and getattr(S.broker, "dry_run", False))
                else ("LIVE" if S.live else "PAPER"))
        col = "yellow" if mode == "DRY" else ("red" if S.live else "cyan")
        t = Text()
        t.append(f"BTC ${btc:,.2f}  ", style="bold white")
        t.append(f"{chg}   ", style="dim")
        t.append(f"[{mode}]", style=f"bold {col}")
        if S.live and S.funder:
            t.append(f" {S.funder[:6]}..{S.funder[-4:]}", style="dim")
            if S.balance is not None:
                t.append(f" ${S.balance:.2f}", style="green")
        t.append(f"   stake ${S.stake}", style="yellow")
        t.append(f"   {S.msg}", style="dim")
        st = btc_state()
        if st is not None:
            t.append("\nBTC ", style="dim")
            t.append(f"{st['dist']:+.2f}", style=("green" if st["dist"] >= 0 else "red"))
            if S.twap:
                t.append(f"  twap ${S.twap:,.2f}", style="dim")
            t.append(f"  z {st['z']:+.2f}sd", style="dim")
            t.append(f"  vol {st['sig']*1e4:.2f}bp/s", style="dim")
            t.append(f"  travel ${st['travel']:.0f}", style="dim")
            t.append(f"  30s {st['m30']:+.2f}", style=("green" if st["m30"] >= 0 else "red"))
            t.append(f"  60s {st['m60']:+.2f}", style=("green" if st["m60"] >= 0 else "red"))
        fp = fair_p()
        if fp is not None:
            bu = cur_book("up")
            mk_ask = bu.best_ask() if bu else None
            t.append("\nFAIR ", style="dim")
            t.append(f"P(up) {fp*100:3.0f}%", style="bold white")
            if mk_ask:
                gap = fp - mk_ask
                t.append("   mkt ", style="dim")
                t.append(f"{mk_ask:.2f}", style="white")
                t.append("   gap ", style="dim")
                t.append(f"{gap*100:+.0f}c", style="dim")
                # the market is the sharper forecaster (Brier 0.08 vs 0.18), so
                # a gap is evidence the model is wrong, not an opportunity
                t.append("  mkt sharper", style="dim italic")
            t.append(f"   strike ${S.strike:,.0f}" if S.strike else "", style="dim")
        sp = btc_spark()
        if sp or S.klines:
            t.append("\n")
            if S.klines:
                r14 = rsi_last(S.klines)
                col = "red" if r14 >= 70 else ("green" if r14 <= 30 else "white")
                t.append("RSI ", style="dim")
                t.append(f"{r14:3.0f}", style=f"bold {col}")
                t.append("  ", style="dim")
            t.append(sp, style="cyan")
            # what the measurement actually says, so the number is not over-read
            t.append("   measured: RSI<30 next 1m up 48.0% vs 47.9% base", style="dim italic")
        self._set("#top", t)

        self._set("#books", make_books_text())

        # regime
        r = Text()
        reg = S.regime
        style = {"DEAD": "red", "TREND": "red", "CHOP": "green",
                 "CHOP-DOWN": "bright_green", "VOLATILE": "yellow",
                 "QUIET-UP": "dim"}.get(reg, "white")
        r.append("DETECTED STATE\n", style="bold")
        if reg == "WARMUP":
            r.append("WARMUP\n", style="bold yellow")
            r.append(f"need 36s of book history\n", style="dim")
            r.append(f"{bar(getattr(S,'warm',0.0))} {getattr(S,'warm',0)*100:.0f}%\n\n",
                     style="yellow")
        else:
            r.append(f"{reg}\n", style=f"bold {style}")
            br = S.rates or {}
            med = br.get("med")
            if med is not None:
                if med > 0:
                    r.append(f"median {med*100:+.1f}%  most trades up\n", style="green")
                else:
                    r.append(f"median {med*100:+.1f}%  most trades LOSE\n", style="bold red")
            r.append("\n")
            m = S.metrics
            if m:
                r.append(f"travel  {m['travel']:.3f}\n", style="dim")
                r.append(f"effic.  {m['eff']:.2f}  {bar(m['eff'])}\n", style="dim")
                r.append(f"net     {m['net']:+.3f}\n", style="dim")
                r.append(f"revers  {m['revs']:.0f}\n", style="dim")
                r.append(f"drift   {m['drift']*100:+.1f}%\n", style="dim")
        self._set("#regime", r)

        # position
        p = Text()
        p.append("POSITION\n", style="bold")
        if S.pos.side:
            mk = mark_price(S.pos.side) or 0.0
            pnl = (mk - S.pos.entry) * S.pos.qty
            pct = (mk / S.pos.entry - 1) * 100 if S.pos.entry else 0.0
            st = "green" if pnl >= 0 else "red"
            p.append(f"{S.pos.side.upper()}  {S.pos.qty:.1f} sh\n", style="bold")
            p.append(f"entry {S.pos.entry:.3f}\n", style="dim")
            p.append(f"mark  {mk:.3f}\n", style="dim")
            p.append(f"P&L   ${pnl:+.2f} ({pct:+.1f}%)\n", style=f"bold {st}")
            p.append(f"held  {time.time()-S.pos.ts:.0f}s\n", style="dim")
            pr = PRESETS.get(S.preset, PRESETS["SCALP"])
            stop_px = S.pos.entry if S.pos.locked else S.pos.entry * (1 - pr["stop"])
            if not S.stop_on:
                p.append("stop  OFF\n", style="bold yellow")
            elif pr["stop"] <= 0 and not S.pos.locked:
                p.append("stop  none (FREE)\n", style="dim")
            else:
                lbl = "BE stop" if S.pos.locked else "stop"
                near = "bold red" if mk <= stop_px * 1.06 else "red"
                p.append(f"{lbl} {stop_px:.3f}\n", style=near)
            if pr["tp"] > 0:
                tp_px = S.pos.entry * (1 + pr["tp"])
                if tp_px > 0.99:
                    # a +50% target on a 0.78 entry is 1.17 -- it can never fill
                    p.append(f"tp    unreachable\n", style="bold yellow")
                else:
                    p.append(f"tp    {tp_px:.3f} (+{pr['tp']*100:.0f}%)\n", style="green")
        else:
            p.append("flat\n\n", style="dim")
        tot = "green" if S.pnl_session >= 0 else "red"
        p.append(f"session ${S.pnl_session:+.2f}", style=f"bold {tot}")
        if S.trades:
            wins = [t for t in S.trades if t["pnl"] > 0]
            losses = [t for t in S.trades if t["pnl"] <= 0]
            wr = len(wins) / len(S.trades) * 100
            aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
            al = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
            p.append(f"   {len(S.trades)}t {wr:.0f}%w\n", style="dim")
            p.append(f"avg +${aw:.2f} / -${abs(al):.2f}", style="dim")
        self._set("#pos", p)

        # base rates
        a = Text()
        wl = warnings()
        a.append("ALERTS ", style="bold")
        a.append(f"({len(wl)})\n" if wl else "\n", style="dim")
        if not wl:
            a.append("  nothing triggered -- quiet tape\n", style="dim")
        for sev, txt in wl[:4]:
            a.append("  \u2022 ", style=sev)
            a.append(f"{txt}\n", style=sev)
        bv, sv_, tn = vol_window(60)
        tot = bv + sv_
        if tot > 0:
            a.append("  60s volume  ", style="dim")
            a.append(f"buy {bv:.1f}", style="green")
            a.append(" / ", style="dim")
            a.append(f"sell {sv_:.1f}", style="red")
            a.append(f"   cvd {S.cvd:+.1f}\n", style="dim")
        self._set("#alerts", a)

        s = Text()
        if S.rates:
            r = S.rates
            s.append(f"BASE RATES  {S.regime}", style="bold")
            s.append(f"   n={r['n']:,} on 250ms capture\n", style="dim")
            s.append(f"  P(+20% in 60s) {r['r20']*100:>5.1f}%", style="white")
            s.append(f"   P(+50%) {r['r50']*100:>5.1f}%", style="white")
            s.append(f"   median {r['med']*100:>+5.1f}%", style="red" if r["med"] < 0 else "green")
            s.append(f"   mean {r['mean']*100:>+5.1f}%\n", style="dim")
            # the bot's own entry rules, evaluated now
            ch = gate_checks()
            npass = sum(1 for _, ok, _ in ch if ok)
            s.append("YOUR BOT'S GATES", style="bold")
            s.append(f"  {npass}/{len(ch)} pass\n", style="green" if npass == len(ch) else "yellow")
            for lbl, ok, val in ch:
                s.append(f"  {lbl} ", style="dim")
                s.append(f"{val:<5}", style="white")
                s.append("PASS " if ok else "FAIL ", style="green" if ok else "red")
            s.append("\n")
            s.append("Frequencies, not a recommendation. The full gate set "
                     "measured -6.0%/trade.", style="dim italic")
        else:
            s.append("warming up detector...", style="dim")
        self._set("#stats", s)

        # log
        lg = Text()
        if S.trades:
            for tr in S.trades[:6]:
                c = "green" if tr["pnl"] >= 0 else "red"
                lg.append(f"{tr['ts']} {tr['side'].upper():<4} "
                          f"{tr['entry']:.3f}->{tr['exit']:.3f} ", style="dim")
                lg.append(f"{tr['pct']*100:+6.1f}% ${tr['pnl']:+6.2f}\n", style=c)
        else:
            lg.append("no trades yet -- u to buy UP, d to buy DOWN", style="dim")
        self._set("#log", lg)
