"""盘口与风控闸门。

任何一条不通过, 就拒绝下单 —— 这是"最起码不亏"真正落地的地方:
不是靠事后止损, 而是靠事前把不可控敞口挡在门外。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class Book:
    bid: float
    ask: float
    ts: float
    tick: float = 0.01
    bid_size: float = 0.0      # 买一可成交份额
    ask_size: float = 0.0      # 卖一可成交份额

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def is_valid(self) -> bool:
        return 0.0 < self.bid < self.ask < 1.0


@dataclass
class Limits:
    min_leg_usd: float = 1.15          # 单腿最低金额(官方 $1.00 之上留缓冲)
    max_pair_cost: float = 0.985        # ask0 + ask1 上限
    max_book_age_s: float = 5.0         # 盘口最大陈旧度
    min_price: float = 0.02             # 极端价位直接跳过
    max_price: float = 0.98
    max_trade_usd: float = 3.0          # 单笔预算
    max_total_exposure_usd: float = 10.0
    max_daily_loss_usd: float = 1.0
    max_spread_pct: float = 0.05        # 单腿价差上限(流动性真空过滤)
    size_step: float = 0.01


@dataclass
class GateResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)

    def fail(self, reason: str) -> "GateResult":
        # 关键: 必须同时翻转 ok, 否则"登记了拒绝原因但闸门依然放行"
        self.ok = False
        self.reasons.append(reason)
        return self


def check_pair(
    book_0: Book,
    book_1: Book,
    limits: Limits,
    now: float,
    exposure_usd: float = 0.0,
    daily_pnl: float = 0.0,
) -> GateResult:
    """开盘前的全部硬闸门。返回 GateResult(ok, reasons)。"""
    r = GateResult(ok=True)

    for name, b in (("leg0", book_0), ("leg1", book_1)):
        if not b.is_valid():
            r.fail(f"{name}: 盘口非法 bid={b.bid} ask={b.ask}")
            continue
        if (now - b.ts) > limits.max_book_age_s:
            r.fail(f"{name}: 盘口陈旧 {(now - b.ts):.2f}s > {limits.max_book_age_s}s")
        if b.bid < limits.min_price or b.ask > limits.max_price:
            r.fail(f"{name}: 极端价位 bid={b.bid} ask={b.ask}")
        if b.spread > limits.max_spread_pct:
            r.fail(f"{name}: 价差 {b.spread:.4f} > {limits.max_spread_pct}")
        if b.spread > 0 and (b.spread / max(b.mid, 1e-9)) > 0.5:
            r.fail(f"{name}: 相对价差异常 {b.spread / max(b.mid, 1e-9):.2f}")

    pair_cost = book_0.ask + book_1.ask
    if pair_cost > limits.max_pair_cost:
        r.fail(f"配对成本 {pair_cost:.4f} > {limits.max_pair_cost}")

    # 二元互补腿买价之和 >= 1 说明盘口交叉/数据陈旧, 不可作为套利依据
    if book_0.bid + book_1.bid >= 1.0:
        r.fail(f"买价之和 {book_0.bid + book_1.bid:.4f} >= 1.0 (交叉/陈旧)")

    if exposure_usd > limits.max_total_exposure_usd:
        r.fail(f"总敞口 {exposure_usd:.2f} > {limits.max_total_exposure_usd}")

    if daily_pnl <= -abs(limits.max_daily_loss_usd):
        r.fail(f"当日亏损 {daily_pnl:.2f} 触发熔断线 {-abs(limits.max_daily_loss_usd)}")

    return r


def max_shares_for_budget(
    budget_usd: float,
    price_0: float,
    price_1: float,
    limits: Limits,
) -> Tuple[float, List[str]]:
    """在预算内求最大对称份数, 并校验单腿金额红线。"""
    reasons: List[str] = []
    pair = price_0 + price_1
    if pair <= 0:
        return 0.0, ["配对价格非法"]
    raw = budget_usd / pair
    # 必须向下取整: 用 round 会向上进位, 造成"预算 100 却需要 100.0032"这类超限
    shares = math.floor(raw / limits.size_step + 1e-9) * limits.size_step
    shares = round(shares, 6)
    while shares > 0 and shares * pair > budget_usd + 1e-12:
        shares = round(shares - limits.size_step, 6)

    # Polymarket CLOB 官方硬约束: BUY 订单支付金额 (maker amount = price * size) 必须严格 <= 2 位小数 (美分)
    def _is_cent_clean(s, p):
        prod = round(s * p, 6)
        return round(prod, 2) == prod

    if shares > 0 and not (_is_cent_clean(shares, price_0) and _is_cent_clean(shares, price_1)):
        cand = math.floor(shares)
        if cand + 0.5 <= shares and _is_cent_clean(cand + 0.5, price_0) and _is_cent_clean(cand + 0.5, price_1):
            shares = float(cand + 0.5)
        elif cand > 0 and _is_cent_clean(cand, price_0) and _is_cent_clean(cand, price_1):
            shares = float(cand)
        else:
            while shares > 0 and not (_is_cent_clean(shares, price_0) and _is_cent_clean(shares, price_1)):
                shares = round(shares - limits.size_step, 4)

    if shares <= 0:
        return 0.0, ["预算不足一份"]

    leg_0 = round(shares * price_0, 2)
    leg_1 = round(shares * price_1, 2)
    leg_1 = shares * price_1
    if leg_0 < limits.min_leg_usd:
        reasons.append(f"leg0 金额 {leg_0:.4f} < {limits.min_leg_usd}")
    if leg_1 < limits.min_leg_usd:
        reasons.append(f"leg1 金额 {leg_1:.4f} < {limits.min_leg_usd}")
    if shares * pair > budget_usd + 1e-9:
        reasons.append(f"总成本 {shares * pair:.4f} 超预算 {budget_usd}")
    return (0.0 if reasons else shares), reasons
