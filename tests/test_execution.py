"""Broker guards and order shapes. No network: the client is faked at the seam."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import polytrader.broker as pb

PK = "0x" + "11" * 32
TOKEN = "1234567890"


class Accepted:
    ok = True
    status = "matched"
    order_id = "0xorder000001"
    making_amount = Decimal("5.00")
    taking_amount = Decimal("11.63")


class Rejected:
    ok = False
    code = "not_enough_balance"
    message = "not enough balance / allowance"


class FakeClient:
    def __init__(self, response=None, tick="0.01", balance=12_345_678):
        self.response = response or Accepted()
        self.calls = []
        self._tick = Decimal(tick)
        self._balance = balance

    def get_order_book(self, asset_id):
        return SimpleNamespace(tick_size=self._tick)

    def get_balance_allowance(self, asset_type):
        assert asset_type == "COLLATERAL"
        return SimpleNamespace(balance=self._balance)

    def place_market_order(self, **kw):
        self.calls.append(kw)
        return self.response

    def cancel_all(self):
        return {"canceled": []}


def boot(monkeypatch, fake, **env):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_MAX_USDC", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    monkeypatch.setenv("PK", PK)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(pb, "_make_client", lambda key, wallet: fake)
    return pb.LiveBroker()


@pytest.fixture
def broker(monkeypatch):
    fake = FakeClient()
    b = boot(monkeypatch, fake)
    assert b.ok, b.error
    b.fake = fake
    return b


def test_no_key_stays_paper(monkeypatch):
    monkeypatch.delenv("PK", raising=False)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    b = pb.LiveBroker()
    assert b.ok is False
    assert "no PK" in b.error


def test_bad_key_is_reported(monkeypatch):
    monkeypatch.setenv("PK", "0xnope")
    b = pb.LiveBroker()
    assert b.ok is False
    assert b.error.startswith("bad PK")


def test_signer_is_derived_from_the_key(broker):
    from eth_account import Account

    assert broker.addr == Account.from_key(PK).address


def test_funder_defaults_to_the_signer(broker):
    assert broker.funder == broker.addr


def test_funder_env_is_used(monkeypatch):
    b = boot(monkeypatch, FakeClient(), POLY_FUNDER="0xProxy")
    assert b.funder == "0xProxy"


def test_max_usdc_parsing(monkeypatch):
    assert boot(monkeypatch, FakeClient(), POLY_MAX_USDC="12.5").max_usdc == 12.5


def test_bad_max_usdc_means_no_cap(monkeypatch):
    assert boot(monkeypatch, FakeClient(), POLY_MAX_USDC="banana").max_usdc == 0.0


def test_buy_is_refused_over_the_cap(broker):
    broker.max_usdc = 5.0
    ok, detail = broker.buy(TOKEN, 6.0, 0.42)
    assert ok is False
    assert "POLY_MAX_USDC" in detail
    assert broker.fake.calls == []


def test_buy_is_refused_for_a_non_positive_amount(broker):
    assert broker.buy(TOKEN, 0, 0.42)[0] is False


def test_sell_is_refused_without_shares(broker):
    assert broker.sell(TOKEN, 0, 0.42)[0] is False


def test_dry_run_never_touches_the_api(broker):
    broker.dry_run = True
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is True
    assert detail.startswith("DRY buy $5.00 @<=43.0¢")
    assert broker.fake.calls == []


def test_live_buy_carries_a_one_tick_price_ceiling(broker):
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is True and "matched" in detail
    call = broker.fake.calls[0]
    assert call["asset_id"] == TOKEN
    assert call["side"] == "BUY"
    assert call["amount"] == 5.0
    assert call["max_spend"] == 5.0
    assert call["max_price"] == 0.43
    assert call["order_type"] == "FAK"


def test_price_ceiling_is_snapped_to_the_tick_grid(broker, monkeypatch):
    monkeypatch.setattr(
        broker.fake, "get_order_book",
        lambda asset_id: SimpleNamespace(tick_size=Decimal("0.001")),
    )
    broker.buy(TOKEN, 5.0, 0.4205)
    # 0.4205 + 0.001 = 0.4215, half up on a 0.001 grid
    assert repr(broker.fake.calls[0]["max_price"]) == "0.422"


def test_live_buy_ceiling_is_capped_at_099(broker):
    broker.buy(TOKEN, 5.0, 0.995)
    assert broker.fake.calls[0]["max_price"] == 0.99


def test_live_sell_carries_a_one_tick_price_floor(broker):
    ok, detail = broker.sell(TOKEN, 12.5, 0.42)
    assert ok is True and "matched" in detail
    call = broker.fake.calls[0]
    assert call["asset_id"] == TOKEN
    assert call["side"] == "SELL"
    assert call["shares"] == 12.5
    assert call["min_price"] == 0.41
    assert call["order_type"] == "FAK"


def test_live_sell_floor_is_capped_at_001(broker):
    broker.sell(TOKEN, 5.0, 0.005)
    assert broker.fake.calls[0]["min_price"] == 0.01


def test_a_rejected_order_surfaces_the_code_and_message(broker):
    broker.fake.response = Rejected()
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is False
    assert "not_enough_balance" in detail
    assert "allowance" in detail


def test_unknown_tick_refuses_the_order(broker, monkeypatch):
    def boom(asset_id):
        raise RuntimeError("no book")

    monkeypatch.setattr(broker.fake, "get_order_book", boom)
    assert broker.tick(TOKEN) is None
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is False
    assert "tick" in detail


def test_an_api_exception_becomes_a_failed_call(broker, monkeypatch):
    def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(broker.fake, "place_market_order", boom)
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is False
    assert detail.startswith("buy failed:")


def test_accepted_but_unfilled_is_not_a_position(broker):
    broker.fake.response = SimpleNamespace(
        ok=True, status="live", order_id="0xabc",
        making_amount=Decimal("0"), taking_amount=Decimal("0"),
    )
    ok, detail = broker.buy(TOKEN, 5.0, 0.42)
    assert ok is False
    assert "nothing filled" in detail


def test_a_partial_sell_says_so(broker):
    broker.fake.response = SimpleNamespace(
        ok=True, status="matched", order_id="0xabc",
        making_amount=Decimal("5.0"), taking_amount=Decimal("2.5"),
    )
    ok, detail = broker.sell(TOKEN, 10.0, 0.50)
    assert ok is True
    assert "partial 5.00/10.00" in detail


def test_balance_is_reported_in_usdc(broker):
    assert broker.balance() == pytest.approx(12.345678)


def test_balance_returns_none_when_unavailable(broker, monkeypatch):
    def boom(asset_type):
        raise RuntimeError("no account")

    monkeypatch.setattr(broker.fake, "get_balance_allowance", boom)
    assert broker.balance() is None


def test_cancel_all_reports_success(broker):
    assert broker.cancel_all()[0] is True


def test_a_buy_records_the_actual_fill(broker):
    broker.fake.response = SimpleNamespace(
        ok=True, status="matched", order_id="0xabc",
        making_amount=Decimal("4.90"), taking_amount=Decimal("10.0"),
    )
    broker.buy(TOKEN, 5.0, 0.49)
    assert broker.last_fill == 10.0
    assert broker.last_price == pytest.approx(0.49)


def test_a_sell_records_the_actual_fill(broker):
    broker.fake.response = SimpleNamespace(
        ok=True, status="matched", order_id="0xabc",
        making_amount=Decimal("10.0"), taking_amount=Decimal("4.90"),
    )
    broker.sell(TOKEN, 10.0, 0.49)
    assert broker.last_fill == 10.0
    assert broker.last_price == pytest.approx(0.49)


def test_a_rejected_order_leaves_no_fill(broker):
    broker.fake.response = Rejected()
    broker.buy(TOKEN, 5.0, 0.42)
    assert broker.last_fill == 0.0
    assert broker.last_price == 0.0


def test_a_dead_broker_refuses_every_order(monkeypatch):
    monkeypatch.delenv("PK", raising=False)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    b = pb.LiveBroker()
    assert b.buy(TOKEN, 5.0, 0.42)[0] is False
    assert b.sell(TOKEN, 5.0, 0.42)[0] is False


def test_doctor_exits_nonzero_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("PK", raising=False)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    assert pb.doctor() == 1
    out = capsys.readouterr().out
    assert "signer : (none)" in out
    assert "ready  : False" in out
