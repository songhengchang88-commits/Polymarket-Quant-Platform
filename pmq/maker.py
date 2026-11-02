"""Polymarket 做市报价规划器。

为什么是这条路:
- 二元平价套利实测 0/400 (最小 ask 和 1.0010), 已被做市商吃干;
- 但做市商自己是赚钱的 —— 他们赚的就是那 1% 的买卖价差与流动性奖励;
- 我们能做的, 是站到他们那一侧: 双边 post-only 挂单, 赚 1-(bid0+bid1)。

约束(来自官方字段):
- rewardsMinSize: 进入奖励池的最低份额(常见 20 股)
- rewardsMaxSpread: 距中间价的最大偏离(常见 4.5 美分)

风险: 只有一条腿成交 = 裸持方向性仓位。本模块通过"库存上限 + 报价偏斜"控制。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pmq.risk import Book

DEFAULT_MIN_SHARES = 20.0
DEFAULT_MAX_SPREAD_CENTS = 4.5


@dataclass
class MakerConfig:
    budget_usd: float = 20.0
    min_shares: float = 20.0          # 奖励池最低份额
    max_spread_cents: float = 4.5     # 奖励带半宽(美分)
    min_edge_pct: float = 0.3         # 双边价差至少留这么多才报价
    max_inventory_shares: float = 20.0
    size_step: float = 0.01


@dataclass
class QuotePlan:
    market_id: str
    question: str
    mid_yes: float
    bid_yes: float
    bid_no: float
    pair_cost: float
    shares: float
    cost_usd: float
    edge_usd: float
    edge_pct: float
    in_band: bool
    notes: str = ""


def plan_quotes(
    market_id: str,
    question: str,
    book_yes: Book,
    book_no: Book,
    cfg: MakerConfig,
    inventory_yes: float = 0.0,
    inventory_no: float = 0.0,
) -> Optional[QuotePlan]:
    """生成双边报价。返回 None 表示该盘口不值得报价。"""
    if not (book_yes.is_valid() and book_no.is_valid()):
        return None

    mid_yes = (book_yes.bid + book_yes.ask) / 2.0
    mid_no = 1.0 - mid_yes
    half_band = cfg.max_spread_cents / 100.0

    # 默认贴买一挂(保证在奖励带内), 再按库存做偏斜
    skew = 0.0
    if inventory_yes > 0:
        skew = min(half_band / 2, (inventory_yes / max(cfg.max_inventory_shares, 1e-9)) * half_band / 2)
    elif inventory_no > 0:
        skew = -min(half_band / 2, (inventory_no / max(cfg.max_inventory_shares, 1e-9)) * half_band / 2)

    bid_yes = book_yes.bid - skew
    bid_no = book_no.bid + skew

    # 夹到奖励带内
    lo_yes, hi_yes = mid_yes - half_band, mid_yes + half_band
    bid_yes = min(max(bid_yes, lo_yes), hi_yes)
    lo_no, hi_no = mid_no - half_band, mid_no + half_band
    bid_no = min(max(bid_no, lo_no), hi_no)

    # 价格必须落在网格上, 且严格小于 1
    bid_yes = round(min(max(bid_yes, 0.01), 0.99), 4)
    bid_no = round(min(max(bid_no, 0.01), 0.99), 4)

    pair_cost = bid_yes + bid_no
    if pair_cost >= 1.0:
        return None

    edge_pct = (1.0 - pair_cost) * 100.0
    if edge_pct < cfg.min_edge_pct:
        return None

    per_side_budget = cfg.budget_usd / 2.0
    size_by_budget = per_side_budget / max(bid_yes, bid_no, 1e-9)
    shares = max(cfg.min_shares, size_by_budget)
    shares = math.floor(shares / cfg.size_step) * cfg.size_step
    shares = round(shares, 6)
    if shares <= 0:
        return None

    cost_usd = shares * pair_cost
    if cost_usd > cfg.budget_usd:
        shares = math.floor((cfg.budget_usd / pair_cost) / cfg.size_step) * cfg.size_step
        shares = round(shares, 6)
        if shares < cfg.min_shares:
            return None
        cost_usd = shares * pair_cost

    in_band = (abs(bid_yes - mid_yes) <= half_band + 1e-9) and (abs(bid_no - mid_no) <= half_band + 1e-9)
    notes = []
    if shares < cfg.min_shares:
        notes.append("份额低于奖励门槛" if in_band else "")
    if inventory_yes > 0 or inventory_no > 0:
        notes.append(f"库存偏斜 yes={inventory_yes} no={inventory_no}")

    return QuotePlan(
        market_id=market_id,
        question=question,
        mid_yes=round(mid_yes, 4),
        bid_yes=bid_yes,
        bid_no=bid_no,
        pair_cost=round(pair_cost, 4),
        shares=shares,
        cost_usd=round(cost_usd, 4),
        edge_usd=round(shares * (1.0 - pair_cost), 4),
        edge_pct=round(edge_pct, 3),
        in_band=in_band,
        notes="; ".join(n for n in notes if n),
    )


def select_markets(
    markets: List[Dict[str, Any]],
    min_volume: float = 5000.0,
    max_min_size: float = DEFAULT_MIN_SHARES,
    min_days: float = 7.0,
    now: Optional[float] = None,
    max_days: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """筛出"$27 够格 + 有量 + 不会马上到期"的市场。"""
    import datetime as dt
    import time as _time

    now_ts = _time.time() if now is None else now
    out: List[Dict[str, Any]] = []
    for market in markets:
        if market.get("closed") or market.get("archived"):
            continue
        try:
            min_size = float(market.get("rewardsMinSize") or 0)
            volume = float(market.get("volume24hr") or market.get("volumeNum") or 0)
        except (TypeError, ValueError):
            continue
        if min_size <= 0 or min_size > max_min_size:
            continue
        if volume < min_volume:
            continue
        end = market.get("endDate") or market.get("end_date_iso")
        if end:
            try:
                end_ts = dt.datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp()
                remaining_days = (end_ts - now_ts) / 86400.0
                if remaining_days < min_days:
                    continue
                if max_days is not None and remaining_days > max_days:
                    continue
            except ValueError:
                continue
        out.append(market)
    return out
