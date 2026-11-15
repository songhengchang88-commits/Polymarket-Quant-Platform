"""Paper engine spread/expiry tests (local v20261112-1).

Audit: P0-1 fill haircut, P0-2 expiry no settlement, P0-4 param-group
calibre, P0-11 fee defaults to 0, P2 quote rounding. Paper only:
tmp_path ledgers, no network, no live orders, no keys/.env.
"""
from datetime import datetime, timedelta, timezone

from mm_ledger import MICRO, MmLedger
from paper_engine import SELF_FEE_BPS, SELF_HALF_SPREAD_PCT, PaperEngine

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def make_book(mid=0.50, spread=0.02, size=1000.0):
    bid = round(mid - spread / 2, 4)
    ask = round(mid + spread / 2, 4)
    return {
        "best_bid": bid,
        "best_ask": ask,
        "bids": [{"price": bid, "size": size}],
        "asks": [{"price": ask, "size": size}],
        "tick_size": "0.01",
    }


def make_market(end_days=30):
    return {
        "id": "m1",
        "question": "test market?",
        "end_date": (NOW + timedelta(days=end_days)).isoformat(),
        "clob_token_ids": ["YES", "NO"],
        "condition_id": "c1",
    }


def make_books(mid=0.50, spread=0.02):
    return {"YES": make_book(mid, spread), "NO": make_book(mid, spread)}


def make_engine(tmp_path, **kw):
    ledger = MmLedger("paper", str(tmp_path))
    eng = PaperEngine(ledger, seed_usd="200", **kw)
    return ledger, eng


def buys(report):
    return [q for q in report["quotes"] if q["side"] == "BUY"]


def sells(report):
    return [q for q in report["quotes"] if q["side"] == "SELL"]


def test_param_group2_half_spread_constant_is_2_5pct_local():
    assert SELF_HALF_SPREAD_PCT == 2.5


def test_tick_floor_dominates_at_mid_050(tmp_path):
    _, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    report = eng.on_book("m1", make_books(mid=0.50), NOW)
    assert report["state"] == "QUIET"
    b = buys(report)
    assert len(b) == 2
    assert all(q["price_micro"] == 480_000 for q in b)
    assert all(q["shares_micro"] == 20 * MICRO for q in b)


def test_pct_floor_dominates_at_mid_090(tmp_path):
    _, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    report = eng.on_book("m1", make_books(mid=0.90), NOW)
    b = buys(report)
    assert len(b) == 2
    assert all(q["price_micro"] == 870_000 for q in b)


def test_half_spread_pct_is_injectable_and_changes_quote(tmp_path):
    _, eng1 = make_engine(tmp_path / "a", half_spread_pct=1.0)
    eng1.register_market(make_market(), score=90.0)
    r1 = eng1.on_book("m1", make_books(mid=0.90), NOW)
    _, eng2 = make_engine(tmp_path / "b", half_spread_pct=2.5)
    eng2.register_market(make_market(), score=90.0)
    r2 = eng2.on_book("m1", make_books(mid=0.90), NOW)
    assert buys(r1)[0]["price_micro"] == 880_000
    assert buys(r2)[0]["price_micro"] == 870_000


def test_sell_price_rounds_up_to_tick(tmp_path):
    # Sell rounds UP to tick: in the crossing step the YES mid is
    # (0.86+0.87)/2 = 0.865, half = max(0.02, 0.865*2.5%) = 0.021625,
    # sell raw = 0.886625 -> ceil to tick -> 0.89.
    # NOTE: steps are 90s apart so the 60s TRENDING volatility window
    # does not see the 0.865 crossing mid together with the 0.90 mids
    # (otherwise half-spread doubles and the sell price becomes 0.95).
    # NOTE 2: assert on the crossing step's own quotes -- the SELL it
    # places would itself be filled by the next normal book (bid 0.89
    # crosses 0.89), so a third step has no YES position left to sell.
    _, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    eng.on_book("m1", make_books(mid=0.90), NOW)
    cross = {
        "YES": {"best_bid": 0.86, "best_ask": 0.87,
                "bids": [{"price": 0.86, "size": 1000.0}],
                "asks": [{"price": 0.87, "size": 100.0}], "tick_size": "0.01"},
        "NO": make_book(0.90),
    }
    rep2 = eng.on_book("m1", cross, NOW + timedelta(seconds=90))
    assert rep2["fills"], "crossing book should fill"
    assert eng.position_shares_micro("m1", "YES") == 20 * MICRO
    s = [q for q in sells(rep2) if q["token_id"] == "YES"]
    assert len(s) == 1
    assert s[0]["price_micro"] == 890_000
    assert s[0]["shares_micro"] == 20 * MICRO


def test_crossing_fill_applies_queue_haircut_050(tmp_path):
    ledger, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    eng.on_book("m1", make_books(mid=0.50), NOW)
    cross = {
        "YES": {"best_bid": 0.47, "best_ask": 0.48,
                "bids": [{"price": 0.47, "size": 1000.0}],
                "asks": [{"price": 0.48, "size": 20.0}], "tick_size": "0.01"},
        "NO": make_book(0.50),
    }
    rep = eng.on_book("m1", cross, NOW + timedelta(seconds=15))
    assert len(rep["fills"]) == 1
    assert rep["fills"][0]["shares_micro"] == 10 * MICRO
    assert eng.position_shares_micro("m1", "YES") == 10 * MICRO
    assert ledger.get_cash_balance_micro() == 195_200_000


def test_no_crossing_no_fill(tmp_path):
    _, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    eng.on_book("m1", make_books(mid=0.50), NOW)
    no_cross = {
        "YES": {"best_bid": 0.47, "best_ask": 0.49,
                "bids": [{"price": 0.47, "size": 1000.0}],
                "asks": [{"price": 0.49, "size": 100.0}], "tick_size": "0.01"},
        "NO": make_book(0.50),
    }
    rep = eng.on_book("m1", no_cross, NOW + timedelta(seconds=15))
    assert rep["fills"] == []


def test_expiry_30h_is_event_no_new_buys(tmp_path):
    _, eng = make_engine(tmp_path)
    m = make_market()
    m["end_date"] = (NOW + timedelta(hours=30)).isoformat()
    eng.register_market(m, score=90.0)
    rep = eng.on_book("m1", make_books(), NOW)
    assert rep["state"] == "EVENT"
    assert buys(rep) == []


def test_expiry_10h_is_reduce_only_no_new_buys(tmp_path):
    _, eng = make_engine(tmp_path)
    m = make_market()
    m["end_date"] = (NOW + timedelta(hours=10)).isoformat()
    eng.register_market(m, score=90.0)
    rep = eng.on_book("m1", make_books(), NOW)
    assert rep["state"] == "REDUCE_ONLY"
    assert buys(rep) == []


def test_expired_market_is_not_halted_known_defect(tmp_path):
    # KNOWN DEFECT P0-2, asserting actual behaviour: expired market is
    # REDUCE_ONLY, not HALTED, and positions are never settled at 0/1.
    # Desired behaviour is HALTED + settlement; fix the code, then
    # change this test to assert HALTED and zeroed positions.
    _, eng = make_engine(tmp_path)
    m = make_market()
    m["end_date"] = (NOW - timedelta(hours=1)).isoformat()
    eng.register_market(m, score=90.0)
    rep = eng.on_book("m1", make_books(), NOW)
    assert rep["state"] == "REDUCE_ONLY"
    assert rep["state"] != "HALTED"
    assert buys(rep) == []


def test_check_buy_single_market_cap_10pct(tmp_path):
    _, eng = make_engine(tmp_path)
    eng.register_market(make_market(), score=90.0)
    ok, _ = eng._check_buy("m1", 19_000_000)
    assert ok is True
    ok, reason = eng._check_buy("m1", 21_000_000)
    assert ok is False
    assert "10.0%" in reason


def test_seed_deposit_only_once_per_ledger(tmp_path):
    ledger, _ = make_engine(tmp_path)
    assert ledger.get_cash_balance_micro() == 200_000_000
    PaperEngine(ledger, seed_usd="200")
    assert ledger.get_cash_balance_micro() == 200_000_000


def test_fee_bps_defaults_to_zero_known_issue(tmp_path):
    # KNOWN ISSUE P0-11: default fee is 0 and nothing reads the CLOB
    # fee table. Fix: read/measure real fees, then change this test.
    assert SELF_FEE_BPS == 0
    _, eng = make_engine(tmp_path)
    assert eng._fee_bps == 0


def test_fee_bps_injected_is_charged_on_fill(tmp_path):
    ledger, eng = make_engine(tmp_path, fee_bps=100)
    eng.register_market(make_market(), score=90.0)
    eng.on_book("m1", make_books(mid=0.50), NOW)
    cross = {
        "YES": {"best_bid": 0.47, "best_ask": 0.48,
                "bids": [{"price": 0.47, "size": 1000.0}],
                "asks": [{"price": 0.48, "size": 20.0}], "tick_size": "0.01"},
        "NO": make_book(0.50),
    }
    eng.on_book("m1", cross, NOW + timedelta(seconds=15))
    fees = [e for e in ledger.get_cash_entries() if e["kind"] == "fee"]
    assert len(fees) == 1
    assert fees[0]["amount_micro"] == -48_000
    assert ledger.get_cash_balance_micro() == 195_152_000
