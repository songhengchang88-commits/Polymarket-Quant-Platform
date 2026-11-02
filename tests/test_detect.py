import pytest

from pmq import detect, risk
from pmq.fees import CostModel

LIMITS = risk.Limits()
COSTS = CostModel(taker_fee_bps=0.0, gas_usd=0.002)


def _pair(bid_0=0.47, ask_0=0.48, bid_1=0.47, ask_1=0.48, size=100.0, ts=1000.0):
    return (risk.Book(bid=bid_0, ask=ask_0, ts=ts, ask_size=size),
            risk.Book(bid=bid_1, ask=ask_1, ts=ts, ask_size=size))


def test_taker_parity_detected_and_executable():
    b0, b1 = _pair(ask_0=0.48, ask_1=0.48)
    opp, why = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 3.0, now=1000.0)
    assert why is None
    assert opp.kind == detect.TAKER
    assert opp.executable is True
    assert opp.pair_cost == pytest.approx(0.96)
    assert opp.edge_per_share == pytest.approx(0.04)
    assert opp.edge_usd > 0


def test_pair_cost_at_one_is_not_taker_executable():
    b0, b1 = _pair(ask_0=0.50, ask_1=0.50)
    opp, why = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 3.0, now=1000.0)
    # 这里会落到 MAKER 类别, 关键是不能被判为可执行的 taker 套利
    assert opp is None or opp.kind != detect.TAKER


def test_stale_book_rejected():
    b0, b1 = _pair(ask_0=0.48, ask_1=0.48, ts=900.0)
    opp, why = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 3.0, now=1000.0)
    assert opp is None
    assert why == "盘口陈旧"


def test_extreme_price_rejected():
    b0, b1 = _pair(bid_0=0.001, ask_0=0.002, ts=1000.0)
    opp, why = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 3.0, now=1000.0)
    assert opp is None
    assert why == "极端价位"


def test_depth_caps_shares():
    b0, b1 = _pair(ask_0=0.48, ask_1=0.48, size=2.0)
    opp, _ = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 100.0, now=1000.0)
    assert opp.shares == pytest.approx(2.0)


def test_reverse_premium_is_not_executable():
    b0, b1 = _pair(bid_0=0.60, ask_0=0.61, bid_1=0.45, ask_1=0.46)
    opp, why = detect.classify_pair("m", "q", b0, b1, LIMITS, COSTS, 3.0, now=1000.0)
    assert why is None
    assert opp.kind == detect.REVERSE
    assert opp.executable is False


def test_fees_can_kill_the_edge():
    b0, b1 = _pair(ask_0=0.48, ask_1=0.48)
    expensive = CostModel(taker_fee_bps=2000, gas_usd=0.002)  # 20% 费率
    opp, _ = detect.classify_pair("m", "q", b0, b1, LIMITS, expensive, 3.0, now=1000.0)
    assert opp.kind == detect.TAKER
    assert opp.executable is False


def test_detect_aggregates_report():
    markets = [{"id": "1", "question": "a"}, {"id": "2", "question": "b"}]
    b0, b1 = _pair(ask_0=0.48, ask_1=0.48)
    books = {"t1-0": b0, "t1-1": b1}

    def tokens(m):
        return ("t1-0", "t1-1") if m["id"] == "1" else ("t2-0", "t2-1")

    report = detect.detect(markets, books, tokens, LIMITS, COSTS, 3.0, now=1000.0)
    assert report.scanned == 2
    assert report.with_books == 1
    assert report.actionable == 1
    assert report.rejected.get("盘口缺失") == 1
