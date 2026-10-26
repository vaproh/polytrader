# Running the trader locally

## 1. Install

```bash
cd polytrader
uv sync
```

`uv sync` creates `.venv` from `pyproject.toml` and `uv.lock`. Every command
below runs through `uv run`; the `justfile` has the same recipes.

## 2. Paper mode first (no key needed)

```bash
uv run python trader.py
```

Reads live Polymarket + Binance. Buys and sells are simulated at the book.
Everything on screen is real except the fills.

Leave it running through at least one 5-minute rollover before trusting it.
The detector needs ~31s of book history per window before it names a regime
(it shows a warmup bar until then).

## 3. Credentials

```bash
cp .env.example .env
chmod 600 .env
```

Fill in:

| var | what |
|---|---|
| `PK` | the private key that signs orders |
| `POLY_FUNDER` | the address holding the USDC. Empty means the signer holds it |
| `POLY_MAX_USDC` | optional per-order ceiling. Empty = no cap |

### Which wallet?

There is no signature-type setting any more. `polymarket-client` derives the
wallet type from the key and funder:

- If you deposit to Polymarket normally, your funds sit in a **proxy or deposit
  wallet**. The address shown on your Polymarket profile is the funder, and the
  MetaMask key that signs is a different address. Put the profile address in
  **`POLY_FUNDER`**.
- If you interact with the contracts straight from MetaMask with no proxy,
  leave **`POLY_FUNDER` empty** and the signer is used.

Not sure? Run the doctor: it prints the signer it derived from your key:

```bash
uv run python execution.py
```

If `signer` equals the address holding your funds, leave `POLY_FUNDER` empty.
If they differ, set `POLY_FUNDER` to the funder.

## 4. Allowances: do this in the UI first

**The bot cannot trade until USDC and the conditional-token contracts are
approved.** The simplest way to set them: make one small manual trade on
polymarket.com from the same wallet. That triggers the approvals.

If you skip this, orders fail with an allowance error, not a balance error.

## 5. Dry run

Checks that the key signs, the funder resolves, and the tick size and price
bound are readable. Nothing is signed and nothing is sent, so this is a
credential check, not a signing test. The header reads `DRY`, not `LIVE`.

```bash
uv run python trader.py --dry
```

You should see a `DRY buy $5.00 @<=0.43` style line after pressing `u`.

## 6. Live

```bash
uv run python trader.py --live --stake 5
```

The header turns **red**, shows `[LIVE]`, your truncated funder address and
your USDC balance. If the key is missing or the client fails to initialise it
refuses to go live and starts in paper mode with the reason printed.

## Keys

| key | action |
|---|---|
| `u` | buy UP at the ask |
| `d` | buy DOWN at the ask |
| `s` | sell the position at the bid |
| `x` | flatten + cancel resting orders |
| `1`..`9` | stake $1..$9 |
| `o` | stake up |
| `p` | pause the detector |
| `r` | reload the window |
| `L` | toggle paper / live |
| `q` | quit |

## Safety properties built in

- paper by default; live needs `--live` **and** a working key
- optional `POLY_MAX_USDC` ceiling, only if you set one
- buy limit = displayed ask + 1 tick; sell limit = displayed bid − 1 tick
  (orders use FAK, so an unfilled order dies instead of resting at a bad price)
- price outside 0.01–0.99 refused; under 20s to resolution refused
- one position at a time; `u`/`d` are refused while one is open
- every failure surfaces in the status line instead of silently doing nothing

## Tests

No credentials and no network needed:

```bash
uv sync
uv run pytest
```

The suite covers the exit rules, the regime detector, the broker guards and
config persistence. The broker tests fake the SDK client at `_make_client`, so
they never place an order or read a key.

## Known limits

- The CLOB websocket does not always resubscribe cleanly when the 5-minute
  window rolls. A REST fallback fills the book whenever the socket is quiet for
  more than 3s, so the panel stays correct but at ~2s resolution. Fine for a
  human read, not for sub-second timing.
- Fill price is assumed to be the displayed ask/bid. The broker reports the
  real status in the status line; it does not reconcile partial fills into P&L.
- Selling uses a market order for the exact share count, rounded to 2dp.
