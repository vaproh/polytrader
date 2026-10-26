#!/usr/bin/env python3
"""
Polymarket toolkit: the data access layer, in one place.

Everything here was hand-rolled repeatedly during research. Collected so it can be
reused and exposed to agents.

Verified endpoints (Oct 2026):
  gamma-api.polymarket.com/events?slug=<slug>          market metadata, outcomes, tokens
  gamma-api.polymarket.com/markets?closed=false         live markets (listing)
  clob.polymarket.com/book?token_id=<id>                current L2 book with sizes
  clob.polymarket.com/prices-history?market=<token>     per-minute history (closed mkts)
  data-api.polymarket.com/{activity,positions,closed-positions,trades}?user=<addr>
  data-api.polymarket.com/trades?market=<conditionId>   tick tape

Known gaps (do not pretend otherwise):
  * there is NO historical order-book endpoint. Books are current-only.
  * btc-5m markets are not surfaced by listing endpoints; slugs are deterministic:
    btc-updown-5m-<windowStartUnix>, window = ts..ts+300
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_retry(url, timeout=20, attempts=3, backoff=1.5):
    last = None
    for i in range(attempts):
        try:
            return _get(url, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429):
                time.sleep(backoff * (i + 1))
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(backoff * (i + 1))
    raise last


def _json_field(v):
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


# ---------------------------------------------------------------- markets
def event(slug):
    """Full event record for a slug, or None."""
    try:
        r = _get_retry(f"{GAMMA}/events?slug={slug}")
        return r[0] if isinstance(r, list) and r else None
    except Exception:
        return None


def market(slug):
    """Normalise a market by either an EVENT slug or a MARKET slug.

    These are different namespaces: an event groups several markets, and its slug
    is not the same string. Try the market endpoint first, then fall back to events.
    """
    m = None
    try:
        r = _get_retry(f"{GAMMA}/markets?slug={slug}")
        if isinstance(r, list) and r:
            m = r[0]
    except Exception:
        m = None
    if m is None:
        ev = event(slug)
        if not ev or not ev.get("markets"):
            return None
        m = ev["markets"][0]
    return {
        "slug": m.get("slug"),
        "event_slug": ((m.get("events") or [{}])[0].get("slug")
                       if isinstance(m.get("events"), list) and m.get("events") else None),
        "question": m.get("question"),
        "outcomes": _json_field(m.get("outcomes")),
        "token_ids": _json_field(m.get("clobTokenIds")),
        "outcome_prices": _json_field(m.get("outcomePrices")),
        "closed": m.get("closed"),
        "condition_id": m.get("conditionId"),
        "volume": m.get("volume"),
        "liquidity": m.get("liquidity"),
        "end_date": m.get("endDate"),
        "description": (m.get("description") or "")[:600],
    }


def btc5m_window(ts=None):
    """Current (or given-time) BTC 5m window: (slug, start_ts, end_ts)."""
    now = time.time() if ts is None else ts
    ws = int(now // 300 * 300)
    return f"btc-updown-5m-{ws}", ws, ws + 300


def live_markets(limit=100, tag=None):
    """Live markets. Note: btc-5m markets are NOT surfaced here."""
    q = f"{GAMMA}/markets?closed=false&limit={min(limit,500)}&order=volumeNum&ascending=false"
    if tag:
        q += f"&tag_slug={tag}"
    try:
        r = _get_retry(q)
    except Exception:
        return []
    out = []
    for m in r if isinstance(r, list) else []:
        out.append({
            "slug": m.get("slug"),
            "question": (m.get("question") or "")[:120],
            "outcomes": _json_field(m.get("outcomes")),
            "outcome_prices": _json_field(m.get("outcomePrices")),
            "token_ids": _json_field(m.get("clobTokenIds")),
            "volume": m.get("volumeNum") or m.get("volume"),
            "liquidity": m.get("liquidityNum") or m.get("liquidity"),
            "end_date": m.get("endDate"),
        })
    return out


# ---------------------------------------------------------------- books
def book(token_id):
    """Current L2 book: best bid/ask with sizes, plus depth within 3c."""
    b = _get_retry(f"{CLOB}/book?token_id={token_id}", timeout=8)
    asks = b.get("asks") or []
    bids = b.get("bids") or []
    ba = min(((float(a["price"]), float(a["size"])) for a in asks), default=None)
    bb = max(((float(x["price"]), float(x["size"])) for x in bids), default=None)
    da3 = sum(float(a["size"]) for a in asks if ba and float(a["price"]) <= ba[0] + 0.03)
    db3 = sum(float(x["size"]) for x in bids if bb and float(x["price"]) >= bb[0] - 0.03)
    return {
        "best_bid": bb[0] if bb else None, "bid_size": bb[1] if bb else 0.0,
        "best_ask": ba[0] if ba else None, "ask_size": ba[1] if ba else 0.0,
        "spread": (ba[0] - bb[0]) if (ba and bb) else None,
        "mid": ((ba[0] + bb[0]) / 2) if (ba and bb) else None,
        "ask_depth_3c": da3, "bid_depth_3c": db3,
        "n_ask_levels": len(asks), "n_bid_levels": len(bids),
    }


def both_books(slug):
    """Books for every outcome of a market."""
    m = market(slug)
    if not m:
        return None
    out = {"slug": slug, "outcomes": {}}
    for name, tok in zip(m["outcomes"] or [], m["token_ids"] or []):
        try:
            out["outcomes"][name] = book(tok)
        except Exception as e:
            out["outcomes"][name] = {"error": str(e)}
    return out


def price_history(token_id, start_ts=None, end_ts=None, fidelity=1):
    """Per-minute price points. Works for closed markets; may 403 while live."""
    q = f"{CLOB}/prices-history?market={token_id}&fidelity={fidelity}"
    if start_ts:
        q += f"&startTs={int(start_ts)}"
    if end_ts:
        q += f"&endTs={int(end_ts)}"
    try:
        return _get_retry(q, timeout=25).get("history", [])
    except Exception:
        return []


# ---------------------------------------------------------------- trades / wallets
def trades(condition_id, limit=500):
    try:
        return _get_retry(f"{DATA}/trades?market={condition_id}&limit={min(limit,1000)}")
    except Exception:
        return []


def wallet_activity(address, limit=500):
    try:
        return _get_retry(f"{DATA}/activity?user={address}&limit={min(limit,500)}")
    except Exception:
        return []


def wallet_positions(address, limit=500):
    try:
        return _get_retry(f"{DATA}/positions?user={address}&limit={min(limit,500)}")
    except Exception:
        return []


def wallet_closed(address, limit=500):
    try:
        return _get_retry(f"{DATA}/closed-positions?user={address}&limit={min(limit,500)}")
    except Exception:
        return []


def round_trips(address, limit=500):
    """Pair BUY/SELL per market+outcome into completed round trips."""
    acts = wallet_activity(address, limit)
    if not isinstance(acts, list):
        return []
    from collections import defaultdict
    by = defaultdict(list)
    for a in acts:
        if a.get("type") not in ("TRADE", "trade"):
            continue
        key = (a.get("conditionId") or a.get("market"), a.get("outcome"))
        by[key].append(a)
    trips = []
    for (cid, oc), lst in by.items():
        lst.sort(key=lambda x: x.get("timestamp", 0))
        buys = [x for x in lst if str(x.get("side", "")).upper() == "BUY"]
        sells = [x for x in lst if str(x.get("side", "")).upper() == "SELL"]
        for b, s in zip(buys, sells):
            try:
                bp = float(b.get("price")); sp = float(s.get("price"))
                sz = min(float(b.get("size", 0)), float(s.get("size", 0)))
            except (TypeError, ValueError):
                continue
            if bp <= 0 or sz <= 0:
                continue
            trips.append({
                "market": cid, "outcome": oc, "buy": bp, "sell": sp, "size": sz,
                "cost": round(bp * sz, 4), "profit": round((sp - bp) * sz, 4),
                "ret_pct": round(100 * (sp / bp - 1), 1),
                "hold_s": round(float(s.get("timestamp", 0)) - float(b.get("timestamp", 0)), 1),
            })
    trips.sort(key=lambda x: x["profit"], reverse=True)
    return trips


if __name__ == "__main__":
    import sys
    print("live markets:", len(live_markets(5)))
    slug, ws, we = btc5m_window()
    print("current btc5m window:", slug, time.strftime("%H:%M:%S", time.gmtime(ws)), "UTC")
    m = market(slug)
    if m:
        print("  outcomes:", m["outcomes"], "prices:", m["outcome_prices"])
        bb = both_books(slug)
        for k, v in (bb or {}).get("outcomes", {}).items():
            print(f"  {k}: {v}")
