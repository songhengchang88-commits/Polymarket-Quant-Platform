import pytest

from pmq.markout import MarkoutProbe, judge, ProbeSample
from pmq.risk import Book


def _book(bid, ask, ts=1000.0):
    return Book(bid=bid, ask=ask, ts=ts)


class ScriptedFeed:
    """按调用顺序返回预置盘口。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def __call__(self, t0, t1):
        idx = min(self.calls // 1, len(self.rounds) - 1)
        b0, b1 = self.rounds[idx]
        self.calls += 1
        return b0, b1


def test_markout_negative_when_price_falls():
    feed = ScriptedFeed([
        (_book(0.47, 0.49), _book(0.47, 0.49)),   # entry bid_sum 0.94
        (_book(0.46, 0.48), _book(0.46, 0.48)),   # bid_sum 0.92
    ])
    probe = MarkoutProbe(feed, sleep=lambda s: None)
    res = probe.run("m", "q", "t0", "t1", offsets=[60])
    assert res.entry_bid_sum == pytest.approx(0.94)
    assert res.samples[0].markout_pct == pytest.approx((0.92 - 0.94) / 0.94 * 100, rel=1e-4)
    assert "逆选择" in res.verdict


def test_markout_positive_when_price_recovers():
    feed = ScriptedFeed([
        (_book(0.47, 0.49), _book(0.47, 0.49)),
        (_book(0.48, 0.50), _book(0.48, 0.50)),
    ])
    res = MarkoutProbe(feed, sleep=lambda s: None).run("m", "q", "t0", "t1", offsets=[60])
    assert res.samples[0].markout_pct > 0
    assert "回弹" in res.verdict


def test_markout_neutral_band():
    feed = ScriptedFeed([
        (_book(0.47, 0.49), _book(0.47, 0.49)),
        (_book(0.4710, 0.49), _book(0.4710, 0.49)),
    ])
    res = MarkoutProbe(feed, sleep=lambda s: None).run("m", "q", "t0", "t1", offsets=[60])
    assert "中性" in res.verdict


def test_probe_collects_all_offsets_in_order():
    feed = ScriptedFeed([
        (_book(0.47, 0.49), _book(0.47, 0.49)),
        (_book(0.47, 0.49), _book(0.47, 0.49)),
        (_book(0.46, 0.48), _book(0.47, 0.49)),
        (_book(0.45, 0.47), _book(0.46, 0.48)),
    ])
    res = MarkoutProbe(feed, sleep=lambda s: None).run("m", "q", "t0", "t1", offsets=[60, 30, 15])
    assert [s.seconds for s in res.samples] == [15.0, 30.0, 60.0]


def test_probe_raises_on_missing_book():
    probe = MarkoutProbe(lambda a, b: (None, None), sleep=lambda s: None)
    with pytest.raises(ValueError):
        probe.run("m", "q", "t0", "t1", offsets=[1])


def test_judge_handles_empty():
    assert judge([]) == "无样本"
