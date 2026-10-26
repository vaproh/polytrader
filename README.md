# Polymarket BTC 5-minute: terminal

A terminal-based trading desk for Polymarket's BTC up/down 5-minute markets.
Live order books, a regime detector, enforced exits, and paper/dry/live modes.

The terminal is the product. It does not tell you what to buy; it shows the
state, the measured frequencies behind that state, and your position, and it
gets out of the way.

---

## 1. Quick start

```bash
uv sync

uv run polytrader              # paper, no credentials
uv run polytrader --dry        # credential + price-bound check; nothing sent
uv run polytrader --live       # real orders, starts disarmed (press a first)
uv run polytrader --live --stake 12.50   # ...or start with a size loaded
```

`uv sync` builds `.venv` from `pyproject.toml` and `uv.lock`; every command
below runs through `uv run`. The same actions are in the `justfile`
(`just paper`, `just dry`, `just live`, `just test`).

Live needs a `.env` (see `.env.example`): `PK` (signing key), `POLY_FUNDER`
(account wallet holding the collateral; empty if the signer holds it), and
optionally `POLY_MAX_USDC`. Orders go through `polymarket-client`, the official
SDK, which derives the wallet type from the key and funder. Polymarket now
holds collateral as **pUSD** on Polygon; deposit USDC through the Polymarket
UI and it is wrapped automatically. The bot does not deposit.

**Allowances:** make one small manual trade on polymarket.com from the same
wallet first. Otherwise orders fail on allowance, not balance.

| key | action | | key | action |
|---|---|---|---|---|
| `u` / `d` | buy UP / DOWN at ask | | `1`-`9` | stake $1-9 |
| `s` | sell at bid | | `a` | custom dollar amount |
| `x` | flatten + cancel | | `o` | stake +$1 |
| `p` | pause detector | | `[` `]` | preset prev/next |
| `r` | reload window | | `k` | stop arm/disarm |
| `q` | quit | | `e` | stop/tp for this preset |
| | | | `b` | bell on/off |
| | | | `L` | paper/live |

`a` opens a prompt; the amount is saved to `~/.polytrader.json`. The terminal
starts disarmed (`ORDER -- (press a)`), and `u`/`d` refuse until an amount is
set, so a stray keypress can never fire. The status
line shows `CASH`, the next `ORDER`, the cash `AFTER` that order, and the
per-order cap. The books show how many shares the stake buys at each ask. In
live mode the cash line refreshes every 10 seconds and after every fill.

**Presets** (persisted to `~/.polytrader.json`):

| name | stop | take profit | breakeven arms |
|---|---|---|---|
| `SCALP` | 20% | 50% | +30% |
| `TIGHT` | 15% | 30% | +20% |
| `RUNNER` | 25% | 100% | +40% |
| `FREE` | off | off | off |

`e` opens a risk prompt for the current preset: type a stop % and a TP %
(0 disables that leg). Each preset keeps its own numbers in the local cfg
file, so editing SCALP never touches RUNNER, and nothing is committed to the
repo. `FREE` can carry a real stop/TP this way while defaulting to off. The
position panel shows the effective stop/TP, including your overrides.

Every booked close flashes `FILLED` in the status line and rings the terminal
bell (`b` toggles it), in paper and live mode. Each trade row ends with `L`
or `P` for the mode it closed in.

Exit order at 30 Hz: arm breakeven → take profit → stop. If TP and stop could
both fire on one tick, TP wins. A TP target above 1.00 is labelled `unreachable`
rather than printed as if reachable.

---

## 2. Resolution mechanics

Polymarket resolves against the **Chainlink BTC/USD 60-second TWAP**. The TWAP
at the window open sets the **price to beat**, and the TWAP at the close sets
the **final price**; Up wins when final is greater than or equal to beat. It is
two 60-second lookbacks, not a five-minute average.

The terminal reads the same values from Polymarket's public 5m feed
(`/api/crypto/price-history`), so the price to beat and the current TWAP match
the market page. If that feed is unreachable it falls back to the 60-second mean
of its own Binance BTC ticks times **USDT/USD** (Kraken, refreshed once a
minute). Measured against Polymarket's published values over 24 windows, the
fallback lands within **$3.73 on average and $10.56 worst**; unadjusted Binance
is off by about $125. The screen labels the live source `chainlink` or `proxy`.

Slug: `btc-updown-5m-<windowStartUnixTs>`, window = `ts .. ts+300`.

---

## 3. Architecture

```
Polymarket price-history API ──► S.strike (beat), S.twap (exact, 2s poll)
Binance aggTrade WS ──► S.btc, btc_hist, trade_hist (buy/sell volume, CVD)
                              │
             60s mean × USDT/USD ──► fallback when the exact feed is stale
Polymarket CLOB WS  ──► S.books (tick-exact: every message mutates the book)
                              │
                     250ms bucket (tick overwrites current bucket)
                              │
                   recompute_detector @ 30 Hz
                              │
        regime + measured base rates │ warnings engine │ fair value
                              │
                   check_exits @ 30 Hz (breakeven → TP → stop)
                              │
                   paint @ 10 Hz; book panel on arrival (≤25/s)
```

### Decisions and why

**Detector samples on its own timer, decoupled from painting.** `travel` is a
sum of absolute moves, so it scales with sample density. Raising the paint rate
from 4 to 10 FPS silently rescaled every regime threshold by ~2.5×, caught only
because the numbers moved.

**Book state is tick-exact; only display is throttled.** ~600 messages/second
mutate the book on arrival. Painting each would cost ~78% of a core, so the book
panel repaints at ≤25/s and the rest at 10 Hz. Settles at 5–17% of one core.

**Websocket resubscribe runs on a timer, not on message arrival.** The
subscription is fixed at connect time. Checking for a roll inside the message
loop deadlocks: a dead subscription goes silent, so no message ever arrives to
trigger the check. It blocked forever and every window after the first ran on
2-second REST polling. A 1-second `wait_for` timeout fixed it: streaming now
recovers 3 s after rollover.

**Live orders run on a background task.** Calling the broker synchronously froze
the TUI for the whole signing + HTTP round-trip. The keypress now returns in
~0.7 ms and the position appears when the fill confirms.

**Panels only repaint when their content changes.** Twenty frames per second
rebuilding all six panels, including a log that changes once a minute, cost 38%
of a core. A dirty-check on text and styles brought it to 5%.

**Paper by default.** Live needs `--live` and a working key; otherwise it
refuses and prints why.

---

## 4. Reading the screen

Seven panels, top to bottom:

| panel | meaning |
|---|---|
| **status** | BTC spot, price to beat and source, current TWAP with UP/DOWN and dollar/percent distance, session %, mode, cash/order/after/cap, stake, pause, last action |
| **window** | window slug, countdown and progress bar, price to beat and source, 60s TWAP now with UP/DOWN and distance, Binance spot as context, resolution rule |
| **books** | both outcome tokens, best bid/ask with size, depth within 3c, shares the stake buys, pair bid/ask and overround, fresh/stale quote age |
| **signal** | detected regime, fair P(up) vs market ask, token bid base rates, z-score, tick vol, travel, 30s/60s move, RSI(1m) 6/12/14/24, gate checks |
| **alerts** | mechanical warnings: RSI extremes, volume surge, aggressor split, CVD, divergence, vol expansion, streaks, EMA200, MACD, Bollinger squeeze |
| **pos** | side, size, cost, value, pays-on-win, entry, mark, P&L, stop and TP prices, session P&L, win rate, preset |
| **log** | closed trades with entry/exit, percent, P&L, L/P mode, and the close reason |

The resolution reference is the **60s TWAP at the close**, not spot. The window
panel shows both: `TWAP NOW` decides UP/DOWN against the beat, and `SPOT
(binance)` is context only. The status and window panels label whether the beat
and TWAP came from `chainlink` or the fallback `proxy`.

First ~31 seconds of each window show `WARMUP`: the detector needs about 31 s
of book history before it names a regime. A brand-new window also has an empty book for
~8 s, which is real, not a bug.

RSI(14) uses the one-minute close series. Until 15 closes exist the panel shows
`RSI -- (n/15 bars)`, never a fake 50. If the startup REST seed fails, it retries
every 30 s while the series is short. Once warm, the RSI is printed to one
decimal: a 14-period RSI on 1-minute bars moves slowly on purpose, so it will
not jump around like a tick indicator.

**Base rates are frequencies, not a recommendation.** They are measured on a
250 ms order-book capture and conditioned on the current regime. A positive mean
next to a negative median means the average trade wins while most trades lose.

**Fair value is a sanity check, not an edge.** The barrier model
`P(up) = Φ(ln(S/K) / (σ√T))` uses per-second vol from ticks with an hour-scale
floor, because a quiet two minutes otherwise collapses σ and a one-cent wiggle
produces 57% confidence. Measured on 481 resolved windows at 60 s left, its
Brier was 0.177 against the market ask's 0.082. The market is more than twice as
well calibrated, so the panel labels the gap `mkt sharper`. A gap is evidence
the model is wrong, not that a trade exists.

---

## 5. Tests

```bash
uv sync          # the dev group comes with it
uv run pytest
```

No credentials and no network. The suite covers the exit rules, every detector
regime, the volatility floor, fair value, warnings, window math, book parsing,
config persistence, and the broker guards. The broker tests fake the SDK client
at `_make_client`, so they never place an order or read a key.

---

## 6. Known limits

- CLOB resubscribes ~3 s after rollover, not instantly.
- **Books are empty ~8 s after a new window opens**: a brand-new market has no
  orders. Measured zero at +6.5 s. Not a bug.
- Polymarket data latency **135–200 ms**, uniform with ~7 ms jitter. TCP RTT to
  their edge is 6–9 ms, so not distance; 282 distinct updates/second, so not
  batching. No client change fixes it.
- Fills use the average price the SDK reports. A partial buy books only the
  shares filled, and a partial sell reduces the position instead of closing it.
- **Stops are requests, not guarantees.** 40% of −15% stops gap straight through
  on a binary contract. Worst case stays near −95% at any width.
- One position at a time. Quitting with a live position leaves it open in your
  wallet with no tracker.
- The bot only tracks positions it opened. A position bought manually on
  polymarket.com is invisible to it; `s` says `no position`. Sell that one in
  the Polymarket UI.
- The exact Polymarket TWAP feed can be unreachable. The terminal then falls back
  to the Binance 60s mean times USDT/USD, within $3.73 on average and $10.56
  worst over the 24 windows measured, and labels the source `proxy`. Near the
  money that can move fair value by a few points. `chainlink stale` means the
  last exact value is more than 10s old.
- `POLY_MAX_USDC` is off unless you set it.

---

## 7. File map

| file | purpose |
|---|---|
| **`src/polytrader/`** | the terminal: `state`, `config`, `detector`, `engine`, `feeds/`, `ui/` |
| `src/polytrader/broker.py` | live broker (`polymarket-client`), opt-in; `uv run polytrader-doctor` prints the derived signer |
| `src/polytrader/poly.py` | Polymarket REST helpers: market lookup and book |
| `tests/` | pytest suite |
| `RUN-LOCAL.md` | credentials, allowances, wallet setup |
| `.env.example` | credential template |
| `pyproject.toml`, `uv.lock` | dependencies, pinned |
| `justfile` | the same commands as short recipes |

---

## 8. Data sources

| source | contents |
|---|---|
| `wss://stream.binance.com` aggTrade | BTC spot (USDT), size, maker flag |
| `polymarket.com/api/crypto/price-history` | exact 5m beat and TWAP history |
| `polymarket.com/api/crypto/crypto-price` | exact window open price (beat) |
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | book, price_change, last_trade_price |
| `gamma-api.polymarket.com/markets?slug=` | token IDs for the current window |
| `clob.polymarket.com/book?token_id=` | REST fallback when the socket is quiet |
| `api.kraken.com/0/public/Ticker?pair=USDTZUSD` | USDT/USD basis, fallback only |
| Polymarket `prices.crypto.twap` | authenticated stream, optional alternative |

Closed markets need `&closed=true` on the gamma `markets?slug=` query.

---

## 9. License

MIT, see `LICENSE`.
