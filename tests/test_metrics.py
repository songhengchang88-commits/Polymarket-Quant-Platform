import math

import pytest

from pmq import metrics


def _ev(pnl, ts):
    return {"type": "CLOSE", "pnl": pnl, "ts": ts}


def test_equity_curve_accumulates_only_closed_events():
    events = [
        {"type": "OPEN", "ts": 1},
        _ev(0.05, 2),
        {"type": "FILL_LOCKED", "ts": 3},
        _ev(-0.02, 4),
    ]
    pts = metrics.equity_curve(events, 10.0)
    assert [p.equity for p in pts] == pytest.approx([10.0, 10.05, 10.03])


def test_max_drawdown_reports_absolute_and_pct():
    events = [_ev(1.0, 1), _ev(-2.0, 2), _ev(5.0, 3)]
    pts = metrics.equity_curve(events, 10.0)  # 10 -> 11 -> 9 -> 14
    dd = metrics.max_drawdown(pts)
    assert dd["max_drawdown_usd"] == pytest.approx(2.0)
    assert dd["max_drawdown_pct"] == pytest.approx(2 / 11 * 100, rel=1e-3)


def test_max_drawdown_zero_when_monotonic():
    events = [_ev(1.0, 1), _ev(1.0, 2)]
    pts = metrics.equity_curve(events, 10.0)
    assert metrics.max_drawdown(pts)["max_drawdown_usd"] == 0.0


def test_win_rate_counts_losses_and_breakeven():
    events = [_ev(1.0, 1), _ev(-0.5, 2), _ev(0.0, 3), _ev(2.0, 4)]
    wr = metrics.win_rate(events)
    assert wr == {"trades": 4, "wins": 2, "losses": 1, "flat": 1, "win_rate_pct": 50.0}


def test_win_rate_not_structurally_100_percent():
    events = [_ev(-0.01, 1)] * 3
    assert metrics.win_rate(events)["win_rate_pct"] == 0.0


def test_profit_factor():
    events = [_ev(3.0, 1), _ev(-1.0, 2)]
    assert metrics.profit_factor(events) == pytest.approx(3.0)


def test_profit_factor_infinite_without_losses():
    assert metrics.profit_factor([_ev(1.0, 1)]) == math.inf


def test_sharpe_is_finite_and_zero_on_flat_series():
    assert metrics.sharpe_ratio(metrics.equity_curve([_ev(1.0, 1)], 10.0)) == 0.0
    events = [_ev(1.0, i) for i in range(2, 12)]
    assert math.isfinite(metrics.sharpe_ratio(metrics.equity_curve(events, 10.0)))


def test_summarize_shape():
    events = [_ev(0.1, 1), _ev(-0.05, 2)]
    s = metrics.summarize(events, 10.0)
    assert s["final_equity"] == pytest.approx(10.05)
    assert s["trades"] == 2
    for key in ("max_drawdown_usd", "sharpe", "profit_factor", "return_pct"):
        assert key in s
