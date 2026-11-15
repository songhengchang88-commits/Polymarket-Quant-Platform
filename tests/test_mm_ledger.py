"""Ledger tests (local v20261112-1 mm_ledger.py).

Audit: P1-6 fill/cash are separate commits, not atomic; P0-5 no
exposure limit in the ledger; P0-7 no unrealised/equity valuation in
the ledger; OI-19 sqlite timeout 30s; OI-18 markout dedup key includes
source. tmp_path SQLite only, no network, no live orders.
"""
import pytest

from mm_ledger import (
    MICRO,
    MmLedger,
    compute_markout_micro,
    price_to_micro,
    shares_to_micro,
    usd_to_micro,
)

M = MICRO


def ledger(tmp_path, mode="paper"):
    return MmLedger(mode, str(tmp_path))


def seed(led, amount=200 * M):
    led.record_cash(kind="deposit", amount_micro=amount)
    return led


def make_order(led, price=500_000, shares=20 * M, client_id="paper-m1-1"):
    return led.record_order(
        client_order_id=client_id, market_id="m1", token_id="YES",
        side="BUY", price_micro=price, shares_micro=shares)


def test_usd_price_shares_reject_float(tmp_path):
    with pytest.raises(TypeError):
        usd_to_micro(1.5)
    with pytest.raises(TypeError):
        price_to_micro(0.5)
    with pytest.raises(TypeError):
        shares_to_micro(20.0)
    assert usd_to_micro("200") == 200 * M
    assert usd_to_micro(200) == 200 * M
    assert price_to_micro("0.5") == 500_000


def test_record_order_rejects_float_and_bool(tmp_path):
    led = ledger(tmp_path)
    with pytest.raises(TypeError):
        led.record_order(client_order_id="x1", market_id="m1", token_id="YES",
                         side="BUY", price_micro=0.5, shares_micro=20 * M)
    with pytest.raises(TypeError):
        led.record_order(client_order_id="x2", market_id="m1", token_id="YES",
                         side="BUY", price_micro=500_000, shares_micro=True)


def test_invalid_mode_and_status_rejected(tmp_path):
    with pytest.raises(ValueError):
        MmLedger("bogus", str(tmp_path))
    led = ledger(tmp_path)
    with pytest.raises(ValueError):
        led.record_order(client_order_id="x", market_id="m1", token_id="YES",
                         side="BUY", price_micro=500_000,
                         shares_micro=20 * M, status="weird")


def test_fill_amount_is_integer_micro_and_capped_at_order_size(tmp_path):
    led = ledger(tmp_path)
    oid = make_order(led)
    fid = led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                          side="BUY", price_micro=500_000,
                          shares_micro=10 * M)
    fills = led.get_fills()
    assert len(fills) == 1
    assert fills[0]["amount_micro"] == 5_000_000
    assert fills[0]["id"] == fid
    with pytest.raises(ValueError):
        led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                        side="BUY", price_micro=500_000,
                        shares_micro=15 * M)


def test_fill_on_cancelled_order_rejected(tmp_path):
    led = ledger(tmp_path)
    oid = make_order(led)
    led.update_order_status(oid, "cancelled")
    with pytest.raises(ValueError):
        led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                        side="BUY", price_micro=500_000,
                        shares_micro=1 * M)


def test_fill_on_missing_order_rejected(tmp_path):
    led = ledger(tmp_path)
    with pytest.raises(ValueError):
        led.record_fill(order_id=9999, market_id="m1", token_id="YES",
                        side="BUY", price_micro=500_000,
                        shares_micro=1 * M)


def test_cash_balance_is_sum_and_balance_after_chain_is_consistent(tmp_path):
    led = seed(ledger(tmp_path))
    led.record_cash(kind="fill_settle", amount_micro=-5_000_000)
    led.record_cash(kind="fee", amount_micro=-48_000)
    assert led.get_cash_balance_micro() == 194_952_000
    entries = led.get_cash_entries()
    assert entries[-1]["balance_after_micro"] == 194_952_000
    assert sum(e["amount_micro"] for e in entries) == 194_952_000


def test_fill_alone_does_not_move_cash_known_split(tmp_path):
    # KNOWN SPLIT P1-6: record_fill only inserts into fills; cash moves
    # only if the caller separately calls record_cash. A crash between
    # the two leaves fill-without-cash. Fix: one transaction for
    # fill + cash + order status; then change this test.
    led = seed(ledger(tmp_path))
    oid = make_order(led)
    led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                    side="BUY", price_micro=500_000, shares_micro=10 * M)
    assert led.get_cash_balance_micro() == 200 * M
    assert len(led.get_fills()) == 1


def test_compute_markout_micro_buy_down_is_negative(tmp_path):
    assert compute_markout_micro("BUY", 500_000, 470_000) == -30_000
    assert compute_markout_micro("BUY", 500_000, 530_000) == 30_000
    assert compute_markout_micro("SELL", 500_000, 530_000) == -30_000
    assert compute_markout_micro("SELL", 500_000, 470_000) == 30_000


def test_markout_idempotent_same_fill_horizon_source(tmp_path):
    led = ledger(tmp_path)
    oid = make_order(led)
    fid = led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                          side="BUY", price_micro=500_000,
                          shares_micro=10 * M)
    a = led.record_markout(fill_id=fid, market_id="m1", token_id="YES",
                           side="BUY", mid_at_fill_micro=500_000,
                           mid_at_horizon_micro=470_000, horizon_sec=300,
                           source="paper_sim")
    b = led.record_markout(fill_id=fid, market_id="m1", token_id="YES",
                           side="BUY", mid_at_fill_micro=500_000,
                           mid_at_horizon_micro=470_000, horizon_sec=300,
                           source="paper_sim")
    assert a == b
    assert len(led.get_markout_samples()) == 1


def test_markout_different_source_is_separate_sample_oi18(tmp_path):
    led = ledger(tmp_path)
    oid = make_order(led)
    fid = led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                          side="BUY", price_micro=500_000,
                          shares_micro=10 * M)
    a = led.record_markout(fill_id=fid, market_id="m1", token_id="YES",
                           side="BUY", mid_at_fill_micro=500_000,
                           mid_at_horizon_micro=470_000, horizon_sec=300,
                           source="paper_sim")
    b = led.record_markout(fill_id=fid, market_id="m1", token_id="YES",
                           side="BUY", mid_at_fill_micro=480_000,
                           mid_at_horizon_micro=470_000, horizon_sec=300,
                           source="paper_sim_genmid")
    assert a != b
    assert len(led.get_markout_samples()) == 2
    assert led.median_markout_micro("m1", source="paper_sim") == -30_000
    assert led.median_markout_micro("m1", source="paper_sim_genmid") == -10_000


def test_median_markout_micro_odd_and_even(tmp_path):
    led = ledger(tmp_path)
    oid = make_order(led)
    for mid_h in [470_000, 510_000, 490_000]:
        fid = led.record_fill(order_id=oid, market_id="m1", token_id="YES",
                              side="BUY", price_micro=500_000,
                              shares_micro=1 * M)
        led.record_markout(fill_id=fid, market_id="m1", token_id="YES",
                           side="BUY", mid_at_fill_micro=500_000,
                           mid_at_horizon_micro=mid_h, horizon_sec=300,
                           source="paper_sim")
    assert led.median_markout_micro("m1", source="paper_sim") == -10_000
    assert led.median_markout_micro("no-such-market") is None


def test_paper_and_live_ledgers_are_isolated(tmp_path):
    paper = MmLedger("paper", str(tmp_path))
    live = MmLedger("live", str(tmp_path))
    live.record_order(client_order_id="live-1", market_id="m1",
                      token_id="YES", side="BUY", price_micro=500_000,
                      shares_micro=20 * M)
    assert paper.get_orders() == []
    assert len(live.get_orders()) == 1
    with pytest.raises(ValueError):
        paper.record_fill(order_id=1, market_id="m1", token_id="YES",
                          side="BUY", price_micro=500_000,
                          shares_micro=1 * M, mode="live")
