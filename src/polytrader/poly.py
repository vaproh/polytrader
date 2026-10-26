"""Polymarket REST access used by the terminal: market metadata and books."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
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
