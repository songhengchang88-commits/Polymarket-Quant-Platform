"""绩效度量: 权益曲线 / 回撤 / 胜率 / 盈亏比 / 夏普。

关键点: 只吃账本里"已结算"的事件, 并且用成交回执产生的 pnl 字段,
从根上避免把"模型预期"混进收益曲线。
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence

CLOSE_TYPES = ("CLOSE", "REALIZED", "FILL_CLOSED")


@dataclass
class EquityPoint:
    ts: float
    equity: float
    pnl: float = 0.0


def _closes(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [e for e in events if e.get("type") in CLOSE_TYPES and "pnl" in e]
    rows.sort(key=lambda e: float(e.get("ts", 0.0)))
    return rows


def equity_curve(events: Iterable[Dict[str, Any]], initial_balance: float) -> List[EquityPoint]:
    points = [EquityPoint(ts=0.0, equity=float(initial_balance))]
    equity = float(initial_balance)
    for row in _closes(events):
        pnl = float(row.get("pnl", 0.0) or 0.0)
        equity += pnl
        points.append(EquityPoint(ts=float(row.get("ts", 0.0)), equity=equity, pnl=pnl))
    return points


def max_drawdown(points: Sequence[EquityPoint]) -> Dict[str, float]:
    """返回绝对回撤与百分比回撤(正数表示回撤幅度)。"""
    peak = -math.inf
    worst_abs = 0.0
    worst_pct = 0.0
    for p in points:
        peak = max(peak, p.equity)
        dd = peak - p.equity
        if dd > worst_abs:
            worst_abs = dd
            worst_pct = (dd / peak) if peak > 0 else 0.0
    return {"max_drawdown_usd": round(worst_abs, 6), "max_drawdown_pct": round(worst_pct * 100, 4)}


def trade_returns(points: Sequence[EquityPoint]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(points, points[1:]):
        if prev.equity > 0:
            out.append(cur.pnl / prev.equity)
    return out


def sharpe_ratio(points: Sequence[EquityPoint], risk_free: float = 0.0) -> float:
    rets = trade_returns(points)
    if len(rets) < 2:
        return 0.0
    excess = [r - risk_free for r in rets]
    sd = stats.pstdev(excess)
    if sd == 0:
        return 0.0
    return round((stats.fmean(excess) / sd) * math.sqrt(len(excess)), 4)


def win_rate(events: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    rows = _closes(events)
    total = len(rows)
    wins = sum(1 for r in rows if float(r.get("pnl", 0.0) or 0.0) > 0)
    losses = sum(1 for r in rows if float(r.get("pnl", 0.0) or 0.0) < 0)
    flat = total - wins - losses
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate_pct": round(wins / total * 100, 2) if total else 0.0,
    }


def profit_factor(events: Iterable[Dict[str, Any]]) -> float:
    rows = _closes(events)
    gains = sum(float(r.get("pnl", 0.0) or 0.0) for r in rows if float(r.get("pnl", 0.0) or 0.0) > 0)
    losses = -sum(float(r.get("pnl", 0.0) or 0.0) for r in rows if float(r.get("pnl", 0.0) or 0.0) < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return round(gains / losses, 4)


def summarize(events: Iterable[Dict[str, Any]], initial_balance: float) -> Dict[str, Any]:
    events = list(events)
    points = equity_curve(events, initial_balance)
    final = points[-1].equity
    return {
        "initial_balance": initial_balance,
        "final_equity": round(final, 4),
        "total_pnl": round(final - initial_balance, 4),
        "return_pct": round((final - initial_balance) / initial_balance * 100, 2) if initial_balance else 0.0,
        **win_rate(events),
        **max_drawdown(points),
        "sharpe": sharpe_ratio(points),
        "profit_factor": profit_factor(events),
    }
