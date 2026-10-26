"""Config persistence: presets and stake survive a restart, file is private."""
from __future__ import annotations

import json
import os
import stat

import trader


def test_round_trip_and_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(trader, "CFG_PATH", str(path))

    trader.save_cfg({"preset": "TIGHT", "stake": 7})
    assert trader.load_cfg() == {"preset": "TIGHT", "stake": 7}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_state_picks_up_a_saved_preset(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"preset": "RUNNER", "stake": 3}))
    monkeypatch.setattr(trader, "CFG_PATH", str(path))

    s = trader.State()
    assert s.preset == "RUNNER"
    assert s.stake == 3


def test_unknown_preset_falls_back_to_scalp(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"preset": "MOONSHOT"}))
    monkeypatch.setattr(trader, "CFG_PATH", str(path))

    assert trader.State().preset == "SCALP"


def test_missing_or_corrupt_config_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(trader, "CFG_PATH", str(path))
    assert trader.load_cfg() == {}

    path.write_text("{not json")
    assert trader.load_cfg() == {}


def test_changing_preset_keeps_the_stake(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setattr(trader, "CFG_PATH", str(path))
    trader.S.__init__()
    trader.S.stake = 8

    class Dummy:
        refresh_view = staticmethod(lambda: None)

    trader.TraderApp._set_preset(Dummy(), "TIGHT")

    assert trader.S.preset == "TIGHT"
    assert trader.S.stake == 8
    assert json.loads(path.read_text()) == {"preset": "TIGHT", "stake": 8}
