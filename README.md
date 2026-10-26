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
uv run polytrader --live       # real orders
```

`uv sync` builds `.venv` from `pyproject.toml` and `uv.lock`; every command
below runs through `uv run`. The same actions are in the `justfile`
(`just paper`, `just dry`, `just live`, `just test`).

Live needs a `.env` (see `.env.example`): `PK` (signing key), `POLY_FUNDER`
(proxy or deposit wallet holding funds; empty if the signer holds them), and
optionally `POLY_MAX_USDC`. Orders go through `polymarket-client`, the official
SDK, which derives the wallet type from the key and funder.

**Allowances:** make one small manual trade on polymarket.com from the same
wallet first. Otherwise orders fail on allowance, not balance.

| key | action | | key | action |
|---|---|---|---|---|
| `u` / `d` | buy UP / DOWN at ask | | `1`-`9` | stake $1-9 |
| `s` | sell at bid | | `o` | stake up |
| `x` | flatten + cancel | | `[` `]` | preset prev/next |
| `p` | pause detector | | `k` | stop arm/disarm |
| `r` | reload window | | `L` | paper/live |
| `q` | quit | | | |

**Presets** (persisted to `~/.polytrader.json`):

| name | stop | take profit | breakeven arms |
|---|---|---|---|
| `SCALP` | 20% | 50% | +30% |
| `TIGHT` | 15% | 30% | +20% |
| `RUNNER` | 25% | 100% | +40% |
| `FREE` | off | off | off |

Exit order at 30 Hz: arm breakeven → take profit → stop. If TP and stop could
both fire on one tick, TP wins. A TP target above 1.00 is labelled `unreachable`
rather than printed as if reachable.

---

## 2. Resolution mechanics

Polymarket resolves against the **Chainlink BTC/USD 60-second TWAP**. The TWAP
at the window open sets the **price to beat**, and the TWAP at the close sets
the **final price**; Up wins when final is greater than or equal to beat. It is
two 60-second lookbacks, not a five-minute average.

Chainlink is a **USD** index and Binance quotes USDT, so the terminal takes the
60-second mean of its own BTC ticks and multiplies by **USDT/USD** (Kraken,
refreshed once a minute). Measured against Polymarket's published values over
24 windows, that lands within **$3.73 on average and $10.56 worst**; unadjusted
it is off by about $125. With credentials, `CryptoTwapPriceSpec` from
`polymarket-client` is the exact feed and replaces the proxy.

Slug: `btc-updown-5m-<windowStartUnixTs>`, window = `ts .. ts+300`.

---

## 3. Architecture

```
Binance aggTrade WS ──► S.btc, btc_hist, trade_hist (buy/sell volume, CVD)
                              │
                     60s mean × USDT/USD ──► S.twap, strike
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

Six panels, top to bottom:

| panel | meaning |
|---|---|
| **top** | BTC spot, 60s TWAP, session %, mode, stake, status; distance to strike, z-score, tick vol, travel, 30s/60s move; fair value vs market ask; RSI(14) and a sparkline |
| **books** | both outcome tokens, best bid/ask, size, depth within 3c, pair sum |
| **regime** | detected state with travel, efficiency, net, reversals, drift |
| **pos** | side, size, entry, mark, P&L, stop and TP prices, session P&L, win rate |
| **alerts** | mechanical warnings: RSI extremes, volume surge, aggressor split, CVD, divergence, vol expansion, streaks, EMA200, MACD, Bollinger squeeze |
| **stats** | base rates for the detected regime and the old gate rules, one PASS/FAIL each |

First ~31 seconds of each window show `WARMUP`: the detector needs about 31 s
of book history before it names a regime. A brand-new window also has an empty book for
~8 s, which is real, not a bug.

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
- The strike is a proxy: Binance 60s mean times USDT/USD, within $3.73 on
  average and $10.56 worst over the 24 windows measured. Near the money that can
  move fair value by a few points. The exact Chainlink stream needs credentials.
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
| `wss://stream.binance.com` aggTrade | BTC spot price, size, maker flag |
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | book, price_change, last_trade_price |
| `gamma-api.polymarket.com/markets?slug=` | token IDs for the current window |
| `clob.polymarket.com/book?token_id=` | REST fallback when the socket is quiet |
| `api.kraken.com/0/public/Ticker?pair=USDTZUSD` | USDT/USD basis, refreshed once a minute |
| Polymarket `prices.crypto.twap` | exact Chainlink 60s TWAP, optional, needs `AsyncSecureClient` |

Closed markets need `&closed=true` on the gamma `markets?slug=` query.

---

## 9. License

MIT, see `LICENSE`.
