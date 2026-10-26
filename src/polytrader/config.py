"""Persistent settings: exit preset and stake size."""
from __future__ import annotations

import json
import os

# Exit profiles. stop/tp/be are fractions of entry; 0 disables that leg.
#   stop  sell if mark falls this far below entry
#   tp    sell if mark rises this far above entry
#   be    once mark is up this much, raise the stop to breakeven
PRESETS = {
    "SCALP":  {"stop": 0.20, "tp": 0.50, "be": 0.30},
    "TIGHT":  {"stop": 0.15, "tp": 0.30, "be": 0.20},
    "RUNNER": {"stop": 0.25, "tp": 1.00, "be": 0.40},
    "FREE":   {"stop": 0.00, "tp": 0.00, "be": 0.00},
}

CFG_PATH = os.path.expanduser("~/.polytrader.json")
# Every closed trade the terminal books, appended as one JSON object per line.
# Only the terminal's own fills live here; manual trades placed on the website
# are invisible to the bot and never recorded.
HISTORY_PATH = os.path.expanduser("~/.polytrader-history.jsonl")


def load_cfg():
    try:
        with open(CFG_PATH) as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_cfg(d):
    try:
        with open(CFG_PATH, "w") as fh:
            json.dump(d, fh)
        os.chmod(CFG_PATH, 0o600)
    except Exception:
        pass


def _leg(raw, default):
    """Normalize a stop/tp override to {"by": "pct"|"usd", "v": float}.

    Bare numbers are percents, the pre-dollars format, so old cfg files keep
    working. Anything unparseable falls back to the default leg.
    """
    if isinstance(raw, dict):
        by = raw.get("by", "pct")
        try:
            v = float(raw.get("v"))
        except (TypeError, ValueError):
            return dict(default)
        if by == "usd":
            return {"by": "usd", "v": v} if v > 0 else dict(default)
        return {"by": "pct", "v": v} if v >= 0 else dict(default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return dict(default)
    return {"by": "pct", "v": v} if v >= 0 else dict(default)


def effective_preset(name, risk):
    """Base preset with this preset's local stop/tp overrides applied.

    Overrides live only in the local cfg file, never in the repo, and each
    preset keeps its own numbers. Bad values fall back to the base preset.
    """
    base = PRESETS.get(name, PRESETS["SCALP"])
    eff = {"stop": {"by": "pct", "v": base["stop"]},
           "tp": {"by": "pct", "v": base["tp"]},
           "be": base["be"]}
    ov = (risk or {}).get(name, {})
    if isinstance(ov, dict):
        for key in ("stop", "tp"):
            if key in ov:
                eff[key] = _leg(ov[key], eff[key])
    return eff


def fmt_leg(leg, sign="+"):
    """"+25%" / "+$0.50" for TP lines, "-25%" / "-$0.50" for stop lines."""
    if leg["by"] == "usd":
        return f"{sign}${leg['v']:.2f}"
    return f"{sign}{leg['v'] * 100:.0f}%"


def load_risk(cfg):
    risk = (cfg or {}).get("risk")
    return risk if isinstance(risk, dict) else {}
