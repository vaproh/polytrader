"""Config persistence: presets and stake survive a restart, file is private."""
from __future__ import annotations

import json
import os
import stat

from polytrader import config
from polytrader.state import S, State
from polytrader.ui.app import TraderApp


def test_round_trip_and_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))

    config.save_cfg({"preset": "TIGHT", "stake": 7})
    assert config.load_cfg() == {"preset": "TIGHT", "stake": 7}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_state_picks_up_a_saved_preset(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"preset": "RUNNER", "stake": 3}))
    monkeypatch.setattr(config, "CFG_PATH", str(path))

    s = State()
    assert s.preset == "RUNNER"
    assert s.stake == 3


def test_unknown_preset_falls_back_to_scalp(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"preset": "MOONSHOT"}))
    monkeypatch.setattr(config, "CFG_PATH", str(path))

    assert State().preset == "SCALP"


def test_missing_or_corrupt_config_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))
    assert config.load_cfg() == {}

    path.write_text("{not json")
    assert config.load_cfg() == {}


def test_changing_preset_keeps_the_stake(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(config, "CFG_PATH", str(path))
    S.__init__()
    S.stake = 8

    class Dummy:
        refresh_view = staticmethod(lambda: None)

    TraderApp._set_preset(Dummy(), "TIGHT")

    assert S.preset == "TIGHT"
    assert S.stake == 8
    assert json.loads(path.read_text()) == {
        "preset": "TIGHT", "stake": 8, "risk": {}}


def test_risk_overrides_are_local_and_per_preset():
    from polytrader.config import effective_preset

    risk = {"SCALP": {"stop": 0.10, "tp": 0.60}, "FREE": {"stop": 0.05}}

    scalp = effective_preset("SCALP", risk)
    assert scalp["stop"] == {"by": "pct", "v": 0.10}
    assert scalp["tp"] == {"by": "pct", "v": 0.60}
    assert scalp["be"] == 0.30  # untouched leg stays on the base preset

    runner = effective_preset("RUNNER", risk)
    assert runner == {"stop": {"by": "pct", "v": 0.25},
                      "tp": {"by": "pct", "v": 1.00}, "be": 0.40}

    free = effective_preset("FREE", risk)
    assert free["stop"] == {"by": "pct", "v": 0.05}
    assert free["tp"] == {"by": "pct", "v": 0.0}


def test_dollar_legs_survive_effective_preset():
    from polytrader.config import effective_preset

    risk = {"SCALP": {"stop": {"by": "usd", "v": 0.40},
                      "tp": {"by": "usd", "v": 0.50}}}

    scalp = effective_preset("SCALP", risk)
    assert scalp["stop"] == {"by": "usd", "v": 0.40}
    assert scalp["tp"] == {"by": "usd", "v": 0.50}


def test_bad_risk_overrides_fall_back_to_base():
    from polytrader.config import effective_preset

    assert effective_preset("SCALP", {"SCALP": {"stop": -1, "tp": "x"}}) == {
        "stop": {"by": "pct", "v": 0.20},
        "tp": {"by": "pct", "v": 0.50}, "be": 0.30}
    assert effective_preset("SCALP", None)["stop"] == {"by": "pct", "v": 0.20}
    assert effective_preset("NOPE", {})["stop"] == {"by": "pct", "v": 0.20}
