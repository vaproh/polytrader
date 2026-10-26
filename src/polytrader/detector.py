"""Market-state detector: regime, volatility, fair value, warnings, gates."""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd

from .state import S, cur_book, secs_left

LOOKBACK = 36.0

# measured on 19,857 moments (Aug books). conditional on regime, not a buy call.
BASE_RATES = {
    # Measured on the Oct 3-6 250ms capture: 363,397 observations.
    # r20/r50 = P(bid reaches +20%/+50% within 60s). med = median 60s return.
    # These are observations, not settings: do not hand-tune them.
    "DEAD":      {"r20": 0.086, "r50": 0.086, "mean": -0.196, "med": -0.200, "n": 884},
    "QUIET-UP":  {"r20": 0.126, "r50": 0.028, "mean": -0.013, "med": +0.043, "n": 21309},
    "CHOP":      {"r20": 0.281, "r50": 0.108, "mean": +0.019, "med": +0.044, "n": 140799},
    "CHOP-DOWN": {"r20": 0.308, "r50": 0.192, "mean": +0.124, "med": -0.154, "n": 130178},
    "VOLATILE":  {"r20": 0.234, "r50": 0.136, "mean": +0.112, "med": -0.149, "n": 54471},
    "TREND":     {"r20": 0.129, "r50": 0.078, "mean": +0.219, "med": +0.011, "n": 15756},
}


def recompute_detector():
    """Regime from 250ms buckets, recomputed at 30 Hz.

    Ticks land continuously (hundreds/sec) and overwrite the current bucket, so
    the last value is always the newest price. Math runs over ~144 buckets, not
    over thousands of ticks, which is what keeps this cheap at 30 Hz.
    """
    if len(S.mid_hist) < 4:
        S.regime = "WARMUP"
        S.rates = {}
        return
    now_b = S.mid_hist[-1][0]
    look = int(LOOKBACK * 4)                       # 36s -> 144 buckets
    pts = [(b, m) for b, m in S.mid_hist if b >= now_b - look]
    span = (pts[-1][0] - pts[0][0]) / 4.0 if pts else 0.0
    S.warm = min(1.0, span / (LOOKBACK * 0.85))
    if len(pts) < 6 or span < LOOKBACK * 0.85:
        S.regime = "WARMUP"
        S.rates = {}
        S.metrics = {"travel": float("nan"), "net": float("nan"),
                     "eff": float("nan"), "revs": 0.0, "drift": 0.0,
                     "span": span}
        return

    mid = np.fromiter((m for _, m in pts), dtype=float, count=len(pts))
    d = np.diff(mid)
    travel = float(np.abs(d).sum())
    net = float(mid[-1] - mid[0])
    eff = abs(net) / travel if travel > 1e-12 else 1.0
    revs = int(np.sum(np.sign(d[1:]) != np.sign(d[:-1]))) if len(d) > 1 else 0
    drift = net / mid[0] if mid[0] else 0.0

    if travel < 0.02:
        reg = "DEAD"
    elif eff >= 0.70:
        reg = "TREND"
    elif eff < 0.35:
        reg = "CHOP-DOWN" if net < 0 else "CHOP"
    elif travel >= 0.40:
        reg = "VOLATILE"
    else:
        reg = "QUIET-UP" if net > 0 else "VOLATILE"

    S.regime = reg
    S.rates = BASE_RATES.get(reg, {})
    S.metrics = {"travel": travel, "net": net, "eff": eff, "revs": revs,
                 "drift": drift, "span": span}


def rsi_last(closes, period=14):
    """Wilder RSI on a list of closes. Returns 50.0 until there is enough data."""
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return 50.0
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:period].mean()
    ad = dn[:period].mean()
    for i in range(period, len(d)):
        au = (au * (period - 1) + up[i]) / period
        ad = (ad * (period - 1) + dn[i]) / period
    if ad <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def vol_window(sec):
    """(buy volume, sell volume, trade count) over the last `sec` seconds."""
    now = time.time()
    b = sv = n = 0.0
    for t, q, is_buy in reversed(S.trade_hist):
        if t < now - sec:
            break
        if is_buy:
            b += q
        else:
            sv += q
        n += 1
    return b, sv, int(n)


def twap60(hist=None, seconds=60.0, now=None, min_samples=1):
    """Mean price over the last `seconds`; the window resolution statistic."""
    pts = S.btc_hist if hist is None else hist
    if not pts:
        return None
    end = pts[-1][0] if now is None else now
    vals = [p for t, p in pts if end - seconds <= t <= end and p > 0]
    if len(vals) < min_samples:
        return None
    return sum(vals) / len(vals)


def warnings():
    """Live flags. Every one is mechanical; each carries its own evidence."""
    out = []
    if len(S.klines) < 30:
        return out
    c = np.asarray(S.klines, dtype=float)
    up_ratio = None

    # --- RSI across three periods
    for n in (7, 14, 25):
        r = rsi_series_py(c, n)
        if r is None:
            continue
        if r >= 80:
            out.append(("red", f"RSI{n} {r:.0f} overbought"))
        elif r >= 70:
            out.append(("yellow", f"RSI{n} {r:.0f} hot"))
        elif r <= 20:
            out.append(("green", f"RSI{n} {r:.0f} oversold"))
        elif r <= 30:
            out.append(("cyan", f"RSI{n} {r:.0f} cold"))

    # --- volume: aggregate + aggressor split
    b30, s30, n30 = vol_window(30)
    b120, s120, _ = vol_window(120)
    if n30 >= 8:
        tot30, tot120 = b30 + s30, b120 + s120
        if tot120 > 0 and tot30 > 0:
            # 30s is a quarter of 120s, so 25% would be "normal"
            share = tot30 / tot120
            if share > 0.55 and tot30 > 1.0:
                out.append(("yellow", f"volume surge {share*100:.0f}% of 2m in 30s"))
        if tot30 > 0:
            up_ratio = b30 / tot30
            if up_ratio >= 0.70:
                out.append(("green", f"aggressive BUYING {up_ratio*100:.0f}% of 30s vol"))
            elif up_ratio <= 0.30:
                out.append(("red", f"aggressive SELLING {(1-up_ratio)*100:.0f}% of 30s vol"))

    # --- CVD direction over 60s
    if len(S.cvd_hist) > 20:
        now = S.cvd_hist[-1][0]
        pts = [v for t, v in S.cvd_hist if t >= now - 60]
        if len(pts) > 5:
            d = pts[-1] - pts[0]
            if abs(d) > 0:
                tag = "green" if d > 0 else "red"
                out.append((tag, f"CVD {'+' if d > 0 else ''}{d:.1f} BTC / 60s"))

    # --- divergence: price new high, RSI not
    r14 = rsi_series_py(c, 14)
    if r14 is not None and len(c) >= 40:
        px_now, px_prev = c[-1], c[-20:].max()
        if px_now >= px_prev * 0.9995:
            r_prev = rsi_series_py(c[:-15], 14)
            if r_prev is not None and r14 < r_prev - 3:
                out.append(("yellow", "bearish divergence (price high, RSI lower)"))
        px_lo = c[-20:].min()
        if px_now <= px_lo * 1.0005:
            r_prev = rsi_series_py(c[:-15], 14)
            if r_prev is not None and r14 > r_prev + 3:
                out.append(("yellow", "bullish divergence (price low, RSI higher)"))

    # --- volatility regime shift
    if len(c) >= 60:
        r = np.diff(np.log(c))
        sv = np.std(r[-15:])
        lv = np.std(r[-60:])
        if lv > 0:
            ratio = sv / lv
            if ratio > 1.8:
                out.append(("yellow", f"vol expansion x{ratio:.1f}"))
            elif ratio < 0.45:
                out.append(("dim", f"vol compression x{ratio:.1f}"))

    # --- trend context vs the 200-period mean
    if len(c) >= 200:
        e200 = float(pd.Series(c).ewm(span=200).mean().iloc[-1])
        gap = (c[-1] / e200 - 1) * 100
        if gap > 0.15:
            out.append(("green", f"above EMA200 by {gap:.2f}%"))
        elif gap < -0.15:
            out.append(("red", f"below EMA200 by {abs(gap):.2f}%"))

    # --- MACD cross within the last few bars
    if len(c) >= 40:
        ser = pd.Series(c)
        macd = ser.ewm(span=12).mean() - ser.ewm(span=26).mean()
        sigl = macd.ewm(span=9).mean()
        diff = (macd - sigl).values
        if len(diff) > 3:
            if diff[-1] > 0 >= diff[-2] and abs(diff[-1]) > 0:
                out.append(("green", "MACD crossed UP"))
            elif diff[-1] < 0 <= diff[-2]:
                out.append(("red", "MACD crossed DOWN"))

    # --- Bollinger squeeze: a coiled range resolves violently
    if len(c) >= 40:
        ser = pd.Series(c)
        ma = ser.rolling(20).mean()
        sd = ser.rolling(20).std()
        width = (2 * sd / ma).dropna()
        if len(width) > 25:
            pct = float((width.iloc[-1] > width.iloc[-25:]).mean())
            if pct <= 0.15:
                out.append(("yellow", "Bollinger squeeze -- range coiled"))

    # --- streak
    if len(c) >= 6:
        d = np.diff(c[-7:])
        if (d > 0).all():
            out.append(("yellow", f"{len(d)} consecutive up bars"))
        elif (d < 0).all():
            out.append(("yellow", f"{len(d)} consecutive down bars"))

    return out


def rsi_fast(period=14, bucket=5.0):
    """Second-scale RSI over 5s closes from the tick tape.

    RSI(1m) barely moves inside a bar; a 5-minute window needs momentum that
    updates every paint. None until ~80s of ticks exist.
    """
    if not S.btc_hist:
        return None
    now = S.btc_hist[-1][0]
    n = period + 1
    lo = now - n * bucket
    buckets = {}
    for t, p in S.btc_hist:
        if t >= lo:
            buckets[int(t // bucket)] = p
    keys = sorted(buckets)
    if len(keys) < n:
        return None
    return rsi_last([buckets[k] for k in keys[-n:]], period)


def rsi_series_py(c, n=14):
    if len(c) < n + 2:
        return None
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:n].mean()
    ad = dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad <= 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + au / ad))


def btc_sigma(lookback=120.0):
    """Per-SECOND volatility from ticks, not from 1m bars.

    A 5-minute window needs second-scale vol. Using 30 one-minute bars measures
    the last half hour's regime, which is the wrong timescale -- BTC can go from
    dead to violent inside a single window.
    """
    if len(S.btc_hist) < 20:
        return None
    now = S.btc_hist[-1][0]
    buckets = {}
    for t, p in S.btc_hist:
        if t >= now - lookback and p > 0:
            buckets[int(t)] = p          # last price in each whole second
    if len(buckets) < 20:
        return None
    ks = sorted(buckets)
    px = np.array([buckets[k] for k in ks], dtype=float)
    r = np.diff(np.log(px))
    r = r[np.isfinite(r)]
    if len(r) < 15:
        return None
    sd = float(np.std(r))
    if sd <= 0:
        return None
    # FLOOR: a quiet two minutes is not a low-volatility regime. If tick vol
    # comes in far below the hour-scale vol, trust the longer measure -- else a
    # one-cent wiggle produces an absurdly confident P(up).
    floor = None
    if len(S.klines) >= 20:
        bar = np.diff(np.log(np.asarray(S.klines[-31:], dtype=float)))
        bar = bar[np.isfinite(bar)]
        if len(bar) >= 5:
            floor = float(np.std(bar) / np.sqrt(60.0))
    if floor and floor > sd:
        sd = floor
    return sd


def btc_state():
    """Everything the window depends on, expressed in BTC terms."""
    sig = btc_sigma()
    px = S.twap or S.btc
    if sig is None or not S.strike or px <= 0:
        return None
    left = max(1.0, secs_left())
    dist = px - S.strike
    # how many standard deviations of expected travel away from the strike
    move_sd = sig * np.sqrt(left)
    z = float(np.log(px / S.strike) / move_sd) if move_sd > 0 else 0.0
    # expected absolute travel over the remaining time, in dollars
    travel = px * move_sd
    # momentum over the last 30s / 60s of ticks
    now = S.btc_hist[-1][0] if S.btc_hist else 0

    def chg(sec):
        pts = [p for t, p in S.btc_hist if t >= now - sec]
        return (S.btc - pts[0]) if pts else 0.0
    return {"sig": sig, "left": left, "dist": dist, "z": z,
            "travel": travel, "m30": chg(30), "m60": chg(60),
            "p": 0.5 * (1.0 + math.erf(z / np.sqrt(2.0)))}


def fair_p():
    """P(this window resolves UP), from BTC alone.

    Barrier probability under a driftless lognormal walk:
        P(up) = Phi( ln(S/K) / (sigma * sqrt(T)) )
    K = BTC when the window opened, S = BTC now, sigma = realised per-second
    vol from the last 30 one-minute bars, T = seconds left.

    Honest status, measured on 481 resolved windows at 60s left:
        model  Brier 0.177
        market Brier 0.082
    The market is more than twice as well calibrated, so this is NOT an edge --
    betting against the market because the model disagrees loses. It is a
    fair-value readout: what the window is worth if BTC behaves like a
    random walk with recent volatility.
    """
    px = S.twap or S.btc
    if not S.strike or px <= 0 or len(S.klines) < 20:
        return None
    st = btc_state()
    if st is None:
        return None
    sig = st["sig"]
    left = max(1.0, secs_left())
    try:
        z = float(np.log(px / S.strike) / (sig * np.sqrt(left)))
    except (ValueError, ZeroDivisionError):
        return None
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def gate_checks():
    """The original signal-bot's entry rules, evaluated live as pass/fail.

    Volatility >= 0.40, efficiency < 0.35, drift <= +2c, price 0.03-0.95,
    40-250s to close. They are shown because they are the user's own rules --
    NOT because they are profitable. On the October capture the full set scored
    -6.0% per trade; volatility alone was the only gate with any signal.
    """
    m = S.metrics or {}
    tr = m.get("travel", float("nan"))
    ef = m.get("eff", float("nan"))
    dr = m.get("drift", float("nan"))
    bu = cur_book("up")
    px = bu.best_ask() if bu else None
    sl = secs_left()
    return [
        ("vol", tr >= 0.40, "--" if tr != tr else f"{tr:.2f}"),
        ("eff", ef < 0.35, "--" if ef != ef else f"{ef:.2f}"),
        ("drift", dr <= 0.02, "--" if dr != dr else f"{dr*100:+.0f}%"),
        ("px", px is not None and 0.03 <= px <= 0.95, "--" if px is None else f"{px * 100:.1f}¢"),
        ("secs", 40 <= sl <= 250, f"{sl:.0f}s"),
    ]
