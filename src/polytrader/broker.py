"""Live order placement. Opt-in only.

NOTHING here runs unless the terminal is launched with --live AND a key is
present. Paper mode never imports this module's broker.

Credentials come from the environment (or a .env in the working directory or
the project root):

    PK                 wallet private key that SIGNS the orders   (required)
    POLY_FUNDER        proxy/funder address that HOLDS the funds  (optional)
    POLY_MAX_USDC      optional per-order ceiling in USDC. unset = no cap.

`polymarket-client` derives the wallet type from the key and funder: a funder
that differs from the signer is a proxy wallet, no explicit signature type
needed. For a plain EOA leave POLY_FUNDER empty and the signer is used.

Design rules
  * every call returns (ok, detail) -- no exceptions escaping into the render loop
  * an optional USDC ceiling per order (POLY_MAX_USDC). off unless you set it.
  * market orders carry a hard price bound: never above the ask we showed plus
    one tick, never below the bid minus one tick, both FAK
"""
from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

MAX_USDC_DEFAULT = 0.0   # 0 means no cap
SLIPPAGE_TICKS = 1


def _snap(price: float, tick: float) -> float:
    """Snap a price onto the market's tick grid.

    ask + 1 tick lands on 0.43000000000000005 in binary floating point, and the
    CLOB rejects a price that does not conform to the tick size.
    """
    if tick <= 0:
        return price
    t = Decimal(str(tick))
    steps = (Decimal(str(price)) / t).to_integral_value(rounding=ROUND_HALF_UP)
    return float(steps * t)


def _load_dotenv():
    """Read a .env from the working directory or the project root."""
    here = Path(__file__).resolve()
    bases = [Path.cwd()]
    if len(here.parents) > 2:
        bases.append(here.parents[2])
    path = next((b / ".env" for b in bases if (b / ".env").is_file()), None)
    if path is None:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _make_client(private_key: str, wallet: str):
    """Seam for tests: the only call here that touches the network."""
    from polymarket import SecureClient
    return SecureClient.create(private_key=private_key, wallet=wallet)


class LiveBroker:
    """Thin, defensive wrapper over `polymarket-client`."""

    def __init__(self, dry_run: bool = False):
        self.ok = False
        self.dry_run = dry_run
        self.addr = ""
        self.funder = ""
        self.max_usdc = MAX_USDC_DEFAULT
        self.error = ""
        self._client = None
        self.last_fill = 0.0      # shares from the last accepted order
        self.last_price = 0.0     # average fill price from the last accepted order
        _load_dotenv()
        self._boot()

    # ------------------------------------------------------------------ setup
    def _boot(self):
        pk = os.environ.get("PK") or os.environ.get("PRIVATE_KEY")
        if not pk:
            self.error = "no PK in env/.env -- staying paper"
            return
        try:
            self.max_usdc = float(os.environ.get("POLY_MAX_USDC") or 0)
        except ValueError:
            self.max_usdc = 0.0

        try:
            from eth_account import Account
        except ImportError as e:
            self.error = f"missing dep: {e}"
            return
        try:
            self.addr = Account.from_key(pk).address
        except Exception as e:
            self.error = f"bad PK: {str(e)[:40]}"
            return

        # An empty funder means "I am the wallet". Passing the signer keeps the
        # SDK on its EOA path; passing None would make it deploy a deposit
        # wallet, which is a chain transaction we never asked for.
        funder = (os.environ.get("POLY_FUNDER") or "").strip()
        self.funder = funder or self.addr
        try:
            self._client = _make_client(pk, self.funder)
            self.ok = True
        except Exception as e:
            self.error = f"clob init: {str(e)[:60]}"

    # ----------------------------------------------------------------- helpers
    def tick(self, token_id: str):
        """The market's tick size, or None when the book cannot be read.

        Guessing a tick widens the price bound tenfold on a 0.001 market, so an
        unknown tick refuses the order instead.
        """
        try:
            return float(self._client.get_order_book(asset_id=token_id).tick_size)
        except Exception:
            return None

    def balance(self):
        """USDC available to trade, best-effort."""
        try:
            r = self._client.get_balance_allowance(asset_type="COLLATERAL")
            return float(r.balance) / 1e6
        except Exception:
            return None

    # ------------------------------------------------------------------ orders
    def buy(self, token_id: str, usdc: float, ask: float):
        """Market-buy `usdc` worth, refusing to pay far above `ask`."""
        if not self.ok:
            return False, self.error or "broker not ready"
        if usdc <= 0:
            return False, "amount must be positive"
        if self.max_usdc > 0 and usdc > self.max_usdc:
            return False, f"${usdc:.2f} over POLY_MAX_USDC ${self.max_usdc:.2f}"

        tick = self.tick(token_id)
        if tick is None:
            return False, "unknown tick size -- order refused"
        limit = _snap(min(0.99, ask + SLIPPAGE_TICKS * tick), tick)
        if self.dry_run:
            return True, f"DRY buy ${usdc:.2f} @<={limit * 100:.1f}¢"

        try:
            resp = self._client.place_market_order(
                asset_id=token_id, side="BUY", amount=round(usdc, 2),
                max_spend=round(usdc, 2), max_price=limit, order_type="FAK",
            )
            ok, detail = self._interpret(resp, f"buy ${usdc:.2f} @<={limit * 100:.1f}¢")
            if ok and self._record_fill(resp, "BUY") <= 0:
                return False, f"{detail} -- accepted but nothing filled"
            return ok, detail
        except Exception as e:
            return False, f"buy failed: {str(e)[:80]}"

    def sell(self, token_id: str, shares: float, bid: float):
        """Market-sell `shares`, refusing to dump far below `bid`."""
        if not self.ok:
            return False, self.error or "broker not ready"
        if shares <= 0:
            return False, "no shares"
        tick = self.tick(token_id)
        if tick is None:
            return False, "unknown tick size -- order refused"
        limit = _snap(max(0.01, bid - SLIPPAGE_TICKS * tick), tick)
        if self.dry_run:
            return True, f"DRY sell {shares:.2f}sh @>={limit * 100:.1f}¢"
        try:
            resp = self._client.place_market_order(
                asset_id=token_id, side="SELL", shares=round(shares, 2),
                min_price=limit, order_type="FAK",
            )
            ok, detail = self._interpret(resp, f"sell {shares:.2f}sh @>={limit * 100:.1f}¢")
            if ok:
                filled = self._record_fill(resp, "SELL")
                if filled <= 0:
                    return False, f"{detail} -- accepted but nothing filled"
                if filled < shares - 1e-9:
                    detail += f" (partial {filled:.2f}/{shares:.2f})"
            return ok, detail
        except Exception as e:
            return False, f"sell failed: {str(e)[:80]}"

    def cancel_all(self):
        try:
            return True, str(self._client.cancel_all())
        except Exception as e:
            return False, f"cancel: {str(e)[:60]}"

    def _record_fill(self, resp, side):
        """Keep the actual fill, not the displayed quote.

        For a BUY the maker amount is USDC and the taker amount is shares; for
        a SELL it is the other way round. Returns the shares filled.
        """
        self.last_fill = 0.0
        self.last_price = 0.0
        try:
            making = float(resp.making_amount)
            taking = float(resp.taking_amount)
        except Exception:
            return 0.0
        shares, usdc = (taking, making) if side == "BUY" else (making, taking)
        if shares > 0:
            self.last_fill = shares
        if shares > 0 and usdc > 0:
            self.last_price = usdc / shares
        return self.last_fill

    @staticmethod
    def _interpret(resp, label):
        """Map an OrderResponse (accepted or rejected) onto (ok, detail)."""
        if getattr(resp, "ok", False):
            status = getattr(resp, "status", "sent")
            oid = str(getattr(resp, "order_id", "") or "")
            if oid:
                return True, f"{label} -> {status} ({oid[:10]}...)"
            return True, f"{label} -> {status}"
        code = getattr(resp, "code", "unknown")
        msg = getattr(resp, "message", "") or "rejected"
        return False, f"{label}: {code}: {msg[:60]}"


def doctor():
    """Print what a live setup would need, without placing anything."""
    b = LiveBroker(dry_run=True)
    print("signer :", b.addr or "(none)")
    print("funder :", b.funder or "(none)")
    print("ready  :", b.ok)
    if b.error:
        print("error  :", b.error)
    if b.ok:
        bal = b.balance()
        print("usdc   :", "unknown" if bal is None else f"${bal:.2f}")
        print("max/ord:", "no cap" if b.max_usdc <= 0 else f"${b.max_usdc:.2f}")
    return 0 if b.ok else 1
