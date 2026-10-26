"""The Textual application: keys, timers, and the seven panels."""
from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static

from ..config import PRESETS, save_cfg
from ..detector import recompute_detector, twap60
from ..engine import exit_decision, settle_position
from ..feeds.basis import basis_loop
from ..feeds.binance import binance_loop, fetch_klines
from ..feeds.chainlink import chainlink_loop
from ..feeds.clob import clob_loop, rest_fallback_loop
from ..feeds.window import window_loop
from ..state import S, cur_book, mark_price, secs_left
from .panels import (make_alerts_text, make_books_text, make_position_text,
                     make_signal_text, make_status_text, make_trades_text,
                     make_window_text)


class Panel(Static):
    pass


class TraderApp(App):
    TITLE = "polytrader"
    SUB_TITLE = "BTC 5m up/down"
    CSS = """
    Screen { background: #000000; }
    #status { height: 3; }
    #window { height: 8; }
    #books { height: 5; }
    #duo { height: 10; }
    #signal { width: 2fr; height: 100%; }
    #pos { width: 1fr; height: 100%; }
    #alerts { height: 7; }
    #log { height: 1fr; }
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
        yield Panel(id="status", classes="box")
        yield Panel(id="window", classes="box")
        yield Panel(id="books", classes="box")
        with Horizontal(id="duo"):
            yield Panel(id="signal", classes="box")
            yield Panel(id="pos", classes="box")
        yield Panel(id="alerts", classes="box")
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
        asyncio.create_task(chainlink_loop())
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
        """Pull 1m candles once so RSI(14) is valid from the first frame.

        A failed seed used to be permanent: the app started before DNS worked,
        then spent 15 minutes showing the RSI 50 fallback. Retry until the REST
        call succeeds, but never wipe bars that ticks already grew.
        """
        while True:
            ks = await asyncio.to_thread(fetch_klines)
            if len(ks) >= 20:
                S.klines = ks
                return
            if len(S.klines) >= 30:
                return
            await asyncio.sleep(30)

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
        # the TWAP is display math, so keep it live even while paused.
        # The exact Chainlink feed owns the resolution numbers; fall back to
        # the Binance proxy only when the exact value is absent or stale.
        if S.twap_src != "chainlink" or time.time() - S.twap_ts > 10:
            tx = twap60(min_samples=30)
            if tx:
                S.twap = tx * S.usdt_usd
                S.twap_src = "proxy"
        if not S.paused:
            recompute_detector()

        self._set("#status", make_status_text())
        self._set("#window", make_window_text())
        self._set("#books", make_books_text())
        self._set("#signal", make_signal_text())
        self._set("#pos", make_position_text())
        self._set("#alerts", make_alerts_text())
        self._set("#log", make_trades_text())
