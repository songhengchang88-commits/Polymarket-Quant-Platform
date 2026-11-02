"""机会检测器: 只判断"现在有没有可成交的边际", 不产生任何虚拟成交。

三类信号:
- TAKER_PARITY     : 两条腿的卖一价之和 < 1, 立即买入并持有到结算可锁利
- REVERSE_PREMIUM  : 两条腿的买一价之和 > 1, 需要先有持仓才能卖, 不构成新开仓机会
- MAKER_QUOTE     : 双边买一价之和 < 1 且价差足够宽, 做市双边挂单可锁利, 但需两边都成交

只有 TAKER_PARITY 在通过全部闸门后才算 executable。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pmq import risk
from pmq.fees import CostModel

TAKER = "TAKER_PARITY"
REVERSE = "REVERSE_PREMIUM"
MAKER = "MAKER_QUOTE"


@dataclass
class Opportunity:
    market_id: str
    question: str
    kind: str
    executable: bool
    edge_usd: float
    edge_per_share: float
    shares: float
    pair_cost: float
    note: str = ""


@dataclass
class DetectionReport:
    scanned: int = 0
    with_books: int = 0
    actionable: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    opportunities: List[Opportunity] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def merge(self, other: "DetectionReport") -> "DetectionReport":
        self.scanned += other.scanned
        self.with_books += other.with_books
        self.actionable += other.actionable
        for reason, count in other.rejected.items():
            self.rejected[reason] = self.rejected.get(reason, 0) + count
        self.opportunities.extend(other.opportunities)
        return self


def _depth_cap(book_0: risk.Book, book_1: risk.Book) -> Tuple[float, bool]:
    """返回 (可成交份额上限, 深度是否已知)。size=0 视为未知。"""
    sizes = [s for s in (book_0.ask_size, book_1.ask_size) if s and s > 0]
    if not sizes:
        return float("inf"), False
    return min(sizes), True


def classify_pair(
    market_id: str,
    question: str,
    book_0: risk.Book,
    book_1: risk.Book,
    limits: risk.Limits,
    costs: CostModel,
    budget_usd: float,
    now: Optional[float] = None,
) -> Tuple[Optional[Opportunity], Optional[str]]:
    """返回 (机会, 拒绝原因)。两者互斥。"""
    now = time.time() if now is None else now

    for name, b in (("leg0", book_0), ("leg1", book_1)):
        if not b.is_valid():
            return None, "盘口非法"
        if (now - b.ts) > limits.max_book_age_s:
            return None, "盘口陈旧"
        if b.bid < limits.min_price or b.ask > limits.max_price:
            return None, "极端价位"
        if b.spread > limits.max_spread_pct:
            return None, "价差过宽"

    pair_cost = book_0.ask + book_1.ask

    # ---- 1) Taker 平价套利 ----
    if pair_cost <= limits.max_pair_cost:
        shares, why = risk.max_shares_for_budget(budget_usd, book_0.ask, book_1.ask, limits)
        if shares <= 0:
            return None, "预算不足/单腿红线"
        cap, depth_known = _depth_cap(book_0, book_1)
        if depth_known:
            shares = min(shares, cap)
        if shares <= 0:
            return None, "卖一深度不足"
        fee_ps = costs.taker_fee(book_0.ask, 1.0) + costs.taker_fee(book_1.ask, 1.0)
        net_ps = (1.0 - pair_cost) - fee_ps
        edge_usd = shares * net_ps - costs.gas_usd
        note = "" if depth_known else "深度未知(按预算上限估算)"
        return Opportunity(
            market_id=market_id, question=question, kind=TAKER,
            executable=net_ps > 0 and edge_usd > 0,
            edge_usd=round(edge_usd, 6), edge_per_share=round(net_ps, 6),
            shares=round(shares, 4), pair_cost=round(pair_cost, 4), note=note,
        ), None

    # ---- 2) 反向溢价(需已有持仓) ----
    bid_sum = book_0.bid + book_1.bid
    if bid_sum >= 1.0:
        return Opportunity(
            market_id=market_id, question=question, kind=REVERSE,
            executable=False, edge_usd=0.0,
            edge_per_share=round(bid_sum - 1.0, 6),
            shares=0.0, pair_cost=round(bid_sum, 4),
            note="需先持有双腿才能卖出, 不构成新开仓",
        ), None

    # ---- 3) 做市双边挂单 ----
    spread_abs = ((book_0.ask - book_0.bid) + (book_1.ask - book_1.bid)) / 2.0
    if spread_abs >= limits.max_spread_pct / 2.0 and bid_sum < 1.0:
        edge_ps = (1.0 - bid_sum) - costs.taker_fee(book_0.bid, 1.0) - costs.taker_fee(book_1.bid, 1.0)
        shares = budget_usd / max(bid_sum, 1e-9)
        return Opportunity(
            market_id=market_id, question=question, kind=MAKER,
            executable=False,                       # 必须双边真实成交才算数
            edge_usd=round(shares * edge_ps - costs.gas_usd, 6),
            edge_per_share=round(edge_ps, 6),
            shares=round(shares, 4), pair_cost=round(bid_sum, 4),
            note="做市报价, 需两侧挂单均成交",
        ), None

    return None, "无边际"


def detect(
    markets: List[Dict[str, Any]],
    books: Dict[str, risk.Book],
    tokens_of,
    limits: risk.Limits,
    costs: CostModel,
    budget_usd: float,
    now: Optional[float] = None,
) -> DetectionReport:
    report = DetectionReport(scanned=len(markets))
    for market in markets:
        pair = tokens_of(market)
        if not pair:
            report.reject("无法解析 token")
            continue
        book_0 = books.get(pair[0])
        book_1 = books.get(pair[1])
        if book_0 is None or book_1 is None:
            report.reject("盘口缺失")
            continue
        report.with_books += 1
        opp, reason = classify_pair(
            str(market.get("id")), str(market.get("question", ""))[:60],
            book_0, book_1, limits, costs, budget_usd, now,
        )
        if opp is None:
            report.reject(reason or "无边际")
            continue
        report.opportunities.append(opp)
        if opp.executable:
            report.actionable += 1
    return report
