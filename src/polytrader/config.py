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


def effective_preset(name, risk):
    """Base preset with this preset's local stop/tp overrides applied.

    Overrides live only in the local cfg file, never in the repo, and each
    preset keeps its own numbers. Bad values fall back to the base preset.
    """
    base = dict(PRESETS.get(name, PRESETS["SCALP"]))
    ov = (risk or {}).get(name, {})
    if isinstance(ov, dict):
        for key in ("stop", "tp"):
            try:
                v = float(ov.get(key))
            except (TypeError, ValueError):
                continue
            if v >= 0:
                base[key] = v
    return base


def load_risk(cfg):
    risk = (cfg or {}).get("risk")
    return risk if isinstance(risk, dict) else {}
