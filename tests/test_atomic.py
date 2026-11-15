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
        self.panic_calls = 0

    def cancel_all(self):
        self.panic_calls += 1
        return {"canceled": [], "not_canceled": {}}

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
    # leg0 成交, leg1 异常, 随后三次平仓重试全部失败 -> 必须如实上报裸敞口
    client = FakeClient([
        {"status": "matched", "orderID": "a1"},
        RuntimeError("leg1 boom"),
        RuntimeError("sell failed 1"),
        RuntimeError("sell failed 2"),
        RuntimeError("sell failed 3"),
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "UNHEDGED"
    assert res.unhedged_shares > 0
    assert res.has_exposure is True


def test_partial_unwind_reports_residual_exposure(tmp_path):
    """平仓只成交一部分时, 必须如实上报残余裸敞口。

    这是实盘最危险的安全假象: 旧实现只要 filled > 0 就返回 True, 调用方随即把
    unhedged_shares 写成 0.0, 残余敞口被静默抹掉 —— 账面上"已补偿", 实际裸露在市场里。
    """
    client = FakeClient([
        {"status": "matched", "orderID": "a1"},                            # leg0 足额成交 3.06
        RuntimeError("leg1 boom"),                                          # leg1 异常
        {"status": "matched", "size_matched": 1.0, "orderID": "sell1"},     # 第 1 次平仓只成交 1.0 份
        {"status": "unmatched", "orderID": "sell2"},                        # 第 2 次未成交
        {"status": "unmatched", "orderID": "sell3"},                        # 第 3 次未成交
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()
    res = _run(ex, b0, b1)

    assert res.status == "UNHEDGED"
    assert res.filled_shares == pytest.approx(3.06)
    assert res.unhedged_shares == pytest.approx(2.06)
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


def test_exception_on_first_leg_triggers_panic_cancel(tmp_path):
    """超时不等于失败: 交易所可能已收单, 必须全撤并标记 UNKNOWN。"""
    client = FakeClient([RuntimeError("read timed out")])
    ex, journal = _executor(client, tmp_path)
    b0, b1 = _books()

    res = _run(ex, b0, b1)
    assert res.status == "UNKNOWN"
    assert client.panic_calls == 1
    assert "read timed out" in res.reasons[0]
    types = [e["type"] for e in journal.read_all()]
    assert "UNKNOWN" in types and "PANIC_CANCEL" in types


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


# ---------------------------------------------------------------------------
# neg-risk 事件组敞口台账
# ---------------------------------------------------------------------------

def test_group_ledger_blocks_second_order_same_group(tmp_path):
    """同一 neg-risk 事件组连续下单必须受事件组上限约束, 而非被误当成独立分散。"""
    client = FakeClient([
        {"status": "matched", "orderID": "a1"},
        {"status": "matched", "orderID": "b1"},
        {"status": "matched", "orderID": "a2"},
        {"status": "matched", "orderID": "b2"},
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()

    r1 = _run(ex, b0, b1, group_key="cond-1")
    assert r1.status == "LOCKED"
    # 本笔占用 = 3.06 份 × (0.49+0.49) = 2.9988
    assert ex.current_group_exposure("cond-1") == pytest.approx(2.9988)

    # 第二笔: 2.9988 + 2.9988 = 5.9976 > 默认上限 4.0 -> 必须在下单前拒绝
    r2 = _run(ex, b0, b1, group_key="cond-1")
    assert r2.status == "REJECTED"
    assert any("neg-risk 事件组" in x for x in r2.reasons)
    # 拒绝发生在下单之前, 不得产生任何 POST
    assert len(client.posts) == 2


def test_group_ledger_is_per_condition_isolated(tmp_path):
    """不同事件组互不影响: 甲组打满不影响乙组。"""
    client = FakeClient([
        {"status": "matched", "orderID": "a1"}, {"status": "matched", "orderID": "b1"},
        {"status": "matched", "orderID": "a2"}, {"status": "matched", "orderID": "b2"},
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()

    assert _run(ex, b0, b1, group_key="cond-A").status == "LOCKED"
    assert _run(ex, b0, b1, group_key="cond-B").status == "LOCKED"
    assert ex.current_group_exposure("cond-A") == pytest.approx(2.9988)
    assert ex.current_group_exposure("cond-B") == pytest.approx(2.9988)


def test_group_ledger_release_frees_capacity(tmp_path):
    """结算/平仓后释放事件组敞口, 同组可再次下单。"""
    client = FakeClient([
        {"status": "matched", "orderID": "a1"}, {"status": "matched", "orderID": "b1"},
        {"status": "matched", "orderID": "a2"}, {"status": "matched", "orderID": "b2"},
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()

    assert _run(ex, b0, b1, group_key="g").status == "LOCKED"
    ex.release_group_exposure("g", 2.9988)
    assert ex.current_group_exposure("g") == pytest.approx(0.0)
    # 释放后同组可再次下单
    assert _run(ex, b0, b1, group_key="g").status == "LOCKED"


def test_group_baseline_from_ledger_counts(tmp_path):
    """链上对账注入的既有敞口必须计入, 否则台账低估真实集中度。"""
    client = FakeClient([
        {"status": "matched", "orderID": "a1"}, {"status": "matched", "orderID": "b1"},
    ])
    ex, _ = _executor(client, tmp_path)
    b0, b1 = _books()

    ex.set_group_exposure_baseline("cond-1", 3.0)
    # 3.0 + 2.9988 = 5.9988 > 4.0 -> 拒绝
    assert _run(ex, b0, b1, group_key="cond-1").status == "REJECTED"
    assert client.posts == []


def test_executor_feeds_deadman_on_lock(tmp_path):
    """双腿成交必须喂死人开关, 否则看门狗无法区分"主循环活着"与"卡死"。"""
    from pmq.execution.deadman import DeadManSwitch
    fed = []
    client = FakeClient([{"status": "matched", "orderID": "a1"}, {"status": "matched", "orderID": "b1"}])
    ex, _ = _executor(client, tmp_path)
    ex.deadman = DeadManSwitch(
        cancel_callback=lambda: fed.append("cancel"),
        heartbeat_timeout_sec=5.0, name="t-deadman")
    b0, b1 = _books()

    assert _run(ex, b0, b1).status == "LOCKED"
    ex.deadman.heartbeat = (lambda: fed.append("feed"))      # 探针: 确认会被调用
    assert _run(ex, b0, b1).status == "LOCKED"
    assert "feed" in fed


def test_executor_stop_is_graceful(tmp_path):
    """执行器停机必须走优雅语义, 不得被误当成进程死亡。"""
    from pmq.execution.deadman import DeadManSwitch
    client = FakeClient([])
    ex, _ = _executor(client, tmp_path)
    ex.deadman = DeadManSwitch(cancel_callback=lambda: None,
                               heartbeat_timeout_sec=5.0, name="t-stop")
    ex.stop()
    assert ex.deadman.disarmed is True
    assert ex.deadman._triggered is False
