"""从盘口序列计算 markout(逆选择)。

逻辑: 把每个采样点当作"假设在买一价被动成交"的入场, 找到 t+horizon 之后
最接近的采样, 用 bid_sum 的变化衡量这段持仓的浮动盈亏。

之所以用 bid_sum 而不是 mid: 出场时你卖的是买一价, 那才是能真正拿到的钱。
"""
from __future__ import annotations

import statistics as stats
from typing import Any, Dict, Iterable, List, Optional, Sequence

from pmq.collect import load_snapshots


def _nearest_after(rows: Sequence[Dict[str, Any]], target_ts: float, tol: float) -> Optional[Dict[str, Any]]:
    best = None
    best_gap = None
    for row in rows:
        gap = abs(float(row["ts"]) - target_ts)
        if gap <= tol and (best_gap is None or gap < best_gap):
            best, best_gap = row, gap
    return best


def markout_rows(
    series: Dict[str, List[Dict[str, Any]]],
    horizons: Iterable[float] = (60.0, 180.0, 300.0),
    tol_ratio: float = 0.25,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for market_id, snaps in series.items():
        if len(snaps) < 2:
            continue
        for i, entry in enumerate(snaps):
            entry_bid = float(entry["bid_0"]) + float(entry["bid_1"])
            if entry_bid <= 0:
                continue
            for horizon in horizons:
                tol = max(5.0, horizon * tol_ratio)
                later = _nearest_after(snaps[i + 1:], float(entry["ts"]) + horizon, tol)
                if later is None:
                    continue
                later_bid = float(later["bid_0"]) + float(later["bid_1"])
                rows.append({
                    "market_id": market_id,
                    "entry_ts": float(entry["ts"]),
                    "horizon": float(horizon),
                    "entry_bid_sum": entry_bid,
                    "exit_bid_sum": later_bid,
                    "markout_pct": (later_bid - entry_bid) / entry_bid * 100.0,
                })
    return rows


def summarize(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_h: Dict[float, List[float]] = {}
    for row in rows:
        by_h.setdefault(float(row["horizon"]), []).append(float(row["markout_pct"]))
    out = []
    for horizon in sorted(by_h):
        values = by_h[horizon]
        neg = sum(1 for v in values if v < 0)
        out.append({
            "horizon": horizon,
            "samples": len(values),
            "mean_pct": round(stats.fmean(values), 4),
            "median_pct": round(stats.median(values), 4),
            "min_pct": round(min(values), 4),
            "max_pct": round(max(values), 4),
            "negative_share": round(neg / len(values) * 100, 1),
        })
    return out


def analyze(path: str, horizons: Sequence[float]) -> List[Dict[str, Any]]:
    series = load_snapshots(path)
    rows = markout_rows(series, horizons=horizons)
    return summarize(rows)
