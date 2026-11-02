import math

import pytest

from pmq.fees import CostModel
from pmq.risk import Book, Limits, check_pair, max_shares_for_budget


def test_taker_fee_is_symmetric_on_probability():
    c = CostModel(taker_fee_bps=100, gas_usd=0.0)  # 1%
    assert c.taker_fee(0.20, 100) == pytest.approx(100 * 0.01 * 0.20)
    assert c.taker_fee(0.80, 100) == pytest.approx(100 * 0.01 * 0.20)


def test_pair_cost_and_locked_edge():
    c = CostModel(taker_fee_bps=0, gas_usd=0.002)
    assert c.pair_cost(0.48, 0.49, 10) == pytest.approx(10 * 0.97 + 0.002)
    assert c.locked_edge(0.48, 0.49, 10) == pytest.approx(10 - 9.702)


def test_locked_edge_goes_negative_with_fees():
    c = CostModel(taker_fee_bps=500, gas_usd=0.002)  # 5% 费率, 足以吃掉 2% 边际
    assert c.locked_edge(0.48, 0.49, 10) < 0


def _books(now=1000.0):
    return Book(bid=0.48, ask=0.49, ts=now), Book(bid=0.49, ask=0.49, ts=now)


def test_gate_passes_on_sane_book():
    b0, b1 = _books()
    r = check_pair(b0, b1, Limits(), now=1000.0)
    assert r.ok, r.reasons


def test_gate_rejects_stale_book():
    b0, b1 = _books(now=990.0)
    r = check_pair(b0, b1, Limits(max_book_age_s=5), now=1000.0)
    assert not r.ok
    assert any("陈旧" in x for x in r.reasons)


def test_gate_rejects_extreme_price():
    b0 = Book(bid=0.001, ask=0.002, ts=1000.0)
    b1 = Book(bid=0.998, ask=0.999, ts=1000.0)
    r = check_pair(b0, b1, Limits(), now=1000.0)
    assert not r.ok
    assert any("极端价位" in x for x in r.reasons)


def test_gate_rejects_crossed_bids():
    b0 = Book(bid=0.60, ask=0.62, ts=1000.0)
    b1 = Book(bid=0.45, ask=0.47, ts=1000.0)
    r = check_pair(b0, b1, Limits(), now=1000.0)
    assert not r.ok
    assert any("交叉" in x for x in r.reasons)


def test_gate_rejects_expensive_pair():
    b0 = Book(bid=0.55, ask=0.57, ts=1000.0)
    b1 = Book(bid=0.44, ask=0.46, ts=1000.0)
    r = check_pair(b0, b1, Limits(), now=1000.0)
    assert not r.ok
    assert any("配对成本" in x for x in r.reasons)


def test_gate_rejects_daily_loss_kill_switch():
    b0, b1 = _books()
    r = check_pair(b0, b1, Limits(max_daily_loss_usd=1.0), now=1000.0, daily_pnl=-1.5)
    assert not r.ok
    assert any("熔断" in x for x in r.reasons)


def test_max_shares_respects_min_leg():
    limits = Limits()
    shares, why = max_shares_for_budget(3.0, 0.03, 0.95, limits)
    assert shares == 0
    assert any("leg0 金额" in w for w in why)


def test_max_shares_happy_path():
    limits = Limits()
    shares, why = max_shares_for_budget(3.0, 0.49, 0.49, limits)
    assert why == []
    assert shares == pytest.approx(3.06)


def test_max_shares_never_exceeds_budget():
    limits = Limits()
    shares, _ = max_shares_for_budget(3.0, 0.49, 0.49, limits)
    assert shares * (0.49 + 0.49) <= 3.0 + 1e-9
