import pytest

from pmq.maker import MakerConfig, plan_quotes, select_markets
from pmq.risk import Book


def _books(bid=0.48, ask=0.50):
    # 二元互补: NO 的买一 = 1 - YES 的卖一
    return (Book(bid=bid, ask=ask, ts=1000.0),
            Book(bid=1 - ask, ask=1 - bid, ts=1000.0))


def test_plan_quotes_joins_bid_and_keeps_edge():
    b_yes, b_no = _books(0.48, 0.50)
    plan = plan_quotes("m", "q", b_yes, b_no, MakerConfig(budget_usd=20.0))
    assert plan is not None
    assert plan.bid_yes == pytest.approx(0.48)
    assert plan.bid_no == pytest.approx(0.50)
    assert plan.pair_cost == pytest.approx(0.98)
    assert plan.edge_pct == pytest.approx(2.0, abs=0.01)
    assert plan.shares >= 20.0
    assert plan.cost_usd <= 20.0 + 1e-9
    assert plan.in_band is True


def test_plan_rejects_when_pair_cost_at_or_above_one():
    b_yes, b_no = _books(0.49, 0.52)  # bid_no = 0.48, sum 0.97 ok
    plan = plan_quotes("m", "q", b_yes, b_no, MakerConfig(budget_usd=20.0))
    assert plan is not None
    # 人为构造无边际盘口
    b_yes2 = Book(bid=0.50, ask=0.51, ts=1000.0)
    b_no2 = Book(bid=0.50, ask=0.51, ts=1000.0)
    assert plan_quotes("m", "q", b_yes2, b_no2, MakerConfig()) is None


def test_plan_caps_size_to_budget():
    b_yes, b_no = _books(0.48, 0.50)
    plan = plan_quotes("m", "q", b_yes, b_no, MakerConfig(budget_usd=10.0, min_shares=5.0))
    assert plan is not None
    assert plan.cost_usd <= 10.0 + 1e-9


def test_inventory_skew_lowers_the_heavy_side():
    b_yes, b_no = _books(0.48, 0.50)
    base = plan_quotes("m", "q", b_yes, b_no, MakerConfig(), inventory_yes=0)
    long_yes = plan_quotes("m", "q", b_yes, b_no, MakerConfig(), inventory_yes=20)
    assert long_yes.bid_yes < base.bid_yes
    assert long_yes.bid_no > base.bid_no


def test_invalid_book_returns_none():
    bad = Book(bid=0.5, ask=0.5, ts=1000.0)
    assert plan_quotes("m", "q", bad, bad, MakerConfig()) is None


def test_select_markets_filters_by_size_volume_and_expiry():
    now = 1_700_000_000.0
    good = {"id": "1", "rewardsMinSize": 20, "volume24hr": 9000,
            "endDate": "2027-01-01T00:00:00Z", "closed": False}
    too_big = {"id": "2", "rewardsMinSize": 100, "volume24hr": 9000,
               "endDate": "2027-01-01T00:00:00Z"}
    too_soon = {"id": "3", "rewardsMinSize": 20, "volume24hr": 9000,
                "endDate": "2023-11-15T00:00:00Z"}
    low_vol = {"id": "4", "rewardsMinSize": 20, "volume24hr": 10,
               "endDate": "2027-01-01T00:00:00Z"}
    out = select_markets([good, too_big, too_soon, low_vol], now=now)
    assert [m["id"] for m in out] == ["1"]
