import pytest

from pmq import risk
from pmq.execution.atomic import AtomicPairExecutor
from pmq.journal import Journal


class FakeClient:
    """按脚本回放回执, 模拟成交/拒单/异常/部分成交。"""

    def __init__(self, script):
        self.script = list(script)
        self.posts = []
        self.cancelled = []

    def create_order(self, args):
        return {"token_id": args.token_id, "price": args.price, "size": args.size, "side": args.side}

    def post_order(self, signed, order_type):
        self.posts.append((signed, order_type))
        if not self.script:
            return {"status": "matched", "orderID": "auto"}
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": True, "orderID": order_id}


def _books(now=1000.0, bid=0.48, ask=0.49):
    return risk.Book(bid=bid, ask=ask, ts=now), risk.Book(bid=bid, ask=ask, ts=now)


def _executor(client, tmp_path):
    journal = Journal(str(tmp_path / "events.jsonl"), fsync=False)
    return AtomicPairExecutor(client, journal=journal), journal


def _run(executor, b0, b1, budget=3.0, **kw):
    return executor.execute(
        market_id="m1", token_0="t0", token_1="t1",
        book_0=b0, book_1=b1, budget_usd=budget, now=kw.pop("now", 1000.0), **kw
    )


def test_both_legs_fill_locks_edge(tmp_path):
    client = FakeClient([{"status": "matched", "orderID": "a1"}, {"status": "matched", "orderID": "b1"}])
    ex, journal = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "LOCKED"
    assert res.filled_shares == pytest.approx(3.06)
    assert res.cash_out == pytest.approx(3.06 * 0.98)
    assert res.locked_edge_usd > 0
    assert len(client.posts) == 2

    types = [e["type"] for e in journal.read_all()]
    assert "FILL_LOCKED" in types


def test_leg0_no_fill_aborts_without_exposure(tmp_path):
    client = FakeClient([{"status": "unmatched", "orderID": "a1"}])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "ABORTED"
    assert res.unhedged_shares == 0.0
    assert "a1" in client.cancelled
    assert len(client.posts) == 1  # 第二腿根本没有发


def test_leg1_reject_triggers_unwind(tmp_path):
    client = FakeClient([
        {"status": "matched", "orderID": "a1"},
        {"status": "unmatched", "orderID": "b1"},
        {"status": "matched", "size_matched": 3.06, "orderID": "sell1"},
    ])
    ex, journal = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "COMPENSATED"
    assert res.unhedged_shares == 0.0
    types = [e["type"] for e in journal.read_all()]
    assert "IMBALANCE" in types


def test_leg1_exception_and_unwind_failure_leaves_unhedged(tmp_path):
    client = FakeClient([
        {"status": "matched", "orderID": "a1"},
        RuntimeError("leg1 boom"),
        RuntimeError("sell failed"),
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "UNHEDGED"
    assert res.unhedged_shares > 0
    assert res.has_exposure is True


def test_partial_fill_on_leg0_is_unwound(tmp_path):
    client = FakeClient([
        {"status": "matched", "size_matched": 1.0, "orderID": "a1"},
        {"status": "matched", "size_matched": 1.0, "orderID": "sell1"},
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "COMPENSATED"
    assert "a1" in client.cancelled


def test_exception_on_first_leg_returns_error_not_raise(tmp_path):
    client = FakeClient([RuntimeError("network down")])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()

    res = _run(ex, b0, b1)
    assert res.status == "ERROR"
    assert "network down" in res.reasons[0]


def test_stale_book_is_rejected_before_any_order(tmp_path):
    client = FakeClient([])
    ex, journal = _executor(client, tmp_path)
    b0, b1 = _books(now=900.0)
    res = _run(ex, b0, b1, now=1000.0)

    assert res.status == "REJECTED"
    assert client.posts == []
    types = [e["type"] for e in journal.read_all()]
    assert "REJECT" in types


def test_pair_cost_above_limit_is_rejected(tmp_path):
    client = FakeClient([])
    ex, _ = _executor(client, tmp_path)
    b0 = risk.Book(bid=0.49, ask=0.50, ts=1000.0)
    b1 = risk.Book(bid=0.48, ask=0.50, ts=1000.0)
    res = _run(ex, b0, b1)

    assert res.status == "REJECTED"
    assert client.posts == []


def test_budget_too_small_for_min_leg_is_rejected(tmp_path):
    client = FakeClient([])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1, budget=1.0)

    assert res.status == "REJECTED"
    assert client.posts == []
