"""盘口与风控闸门。

任何一条不通过, 就拒绝下单 —— 这是"最起码不亏"真正落地的地方:
不是靠事后止损, 而是靠事前把不可控敞口挡在门外。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


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
    # neg-risk 事件组集中度上限。
    # 同一 condition_id 下的多个 outcome 是"按构造相关"的: 一个结果结算,
    # 整组价值可能同时清零。只按"总额敞口"风控会把 8 个名义仓位误判为 8 份独立
    # 分散, 实为对单一事件 8 倍下注。此上限按事件组分别计。
    max_exposure_per_neg_risk_group_usd: float = 4.0
    enforce_group_cap: bool = True


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
    group_key: Optional[str] = None,
    group_exposure_usd: float = 0.0,
    group_add_usd: float = 0.0,
) -> GateResult:
    """开盘前的全部硬闸门。返回 GateResult(ok, reasons)。

    group_* 为 neg-risk 事件组集中度闸门, 全部有默认值, 旧调用方无需改动:
    - group_key: 事件组标识(通常取市场对象的 condition_id); 为空则跳过该闸门;
    - group_exposure_usd: 该事件组当前已占用的敞口;
    - group_add_usd: 本笔订单将追加的敞口。
    """
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

    if limits.enforce_group_cap and group_key and group_add_usd > 0:
        limit = limits.max_exposure_per_neg_risk_group_usd
        after = group_exposure_usd + group_add_usd
        if after > limit + 1e-9:
            r.fail(f"neg-risk 事件组 {group_key} 敞口 {after:.2f} > 上限 {limit} "
                   f"(已占用 {group_exposure_usd:.2f} + 本笔 {group_add_usd:.2f})")

    return r


# Polymarket CLOB 官方取整配置 (对齐 py_clob_client_v2.order_builder.builder.ROUNDING_CONFIG):
#   tick 0.1    -> price 1dp, size 2dp, maker/taker amount 3dp
#   tick 0.01   -> price 2dp, size 2dp, amount 4dp
#   tick 0.005  -> price 3dp, size 2dp, amount 5dp
#   tick 0.001  -> price 3dp, size 2dp, amount 5dp
#   tick 0.0001 -> price 4dp, size 2dp, amount 6dp
# 关键结论 (已用 SDK 实测验证, price=0.49/size=3.06 -> maker=1499400 即 1.4994 USDC 合法):
#   **不存在"maker amount 必须 2 位小数(美分)"这条约束**。
#   只要 price 与 size 各自对齐 tick, 二者乘积的位数天然 <= amount 上限, 永远不会触发下调。
# 旧的 2 位小数硬规则会把 3.06 份/6.38 份之类强行砍到整数份, 白丢最多 ~6% 的部署本金。
CLOB_SIZE_DECIMALS = 2          # 官方 size 一律 2 位小数 (round_down)
CLOB_AMOUNT_DECIMALS_DEFAULT = 4  # 默认按 tick 0.01 的 amount 上限


def clob_amount_decimals(tick: float) -> int:
    """按 tick 返回 CLOB 允许的 maker/taker amount 小数位上限 (对齐官方 ROUNDING_CONFIG)。"""
    try:
        t = float(tick)
    except (TypeError, ValueError):
        return CLOB_AMOUNT_DECIMALS_DEFAULT
    for known, dec in ((0.1, 3), (0.01, 4), (0.005, 5), (0.0025, 6), (0.001, 5), (0.0001, 6)):
        if abs(t - known) < 1e-12:
            return dec
    return CLOB_AMOUNT_DECIMALS_DEFAULT


def _amount_representable(shares: float, price: float, amount_decimals: int) -> bool:
    """maker amount (price * shares) 是否能在官方允许的位数内精确表示。

    不能表示时 SDK 会自行 round_up/round_down, 真实成交价随之漂移 —— 这才是
    "invalid amounts / 价格错位" 的真正根因, 而不是所谓的美分约束。
    """
    prod = float(shares) * float(price)
    return abs(round(prod, amount_decimals) - prod) < 1e-9


def max_shares_for_budget(
    budget_usd: float,
    price_0: float,
    price_1: float,
    limits: Limits,
    tick: float = 0.01,
) -> Tuple[float, List[str]]:
    """在预算内求最大对称份数, 并校验单腿金额红线。

    tick 用于对齐官方 amount 位数上限; 缺省 0.01 (本项目主战场)。
    """
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

    # 官方 size 一律 2 位小数且向下取整, 保证"本地算的份数"与"SDK 实际下单份数"完全一致
    if shares > 0:
        shares = math.floor(shares * (10 ** CLOB_SIZE_DECIMALS) + 1e-9) / (10 ** CLOB_SIZE_DECIMALS)

    # 仅在乘积超出官方位数上限时才下沉 (正常 tick 对齐下永不触发)
    amount_decimals = clob_amount_decimals(tick)
    if shares > 0 and not (_amount_representable(shares, price_0, amount_decimals)
                           and _amount_representable(shares, price_1, amount_decimals)):
        step = 1.0 / (10 ** CLOB_SIZE_DECIMALS)
        guard = 0
        while shares > 0 and guard < 10 ** CLOB_SIZE_DECIMALS * 100 and not (
            _amount_representable(shares, price_0, amount_decimals)
            and _amount_representable(shares, price_1, amount_decimals)
        ):
            shares = round(shares - step, CLOB_SIZE_DECIMALS + 2)
            guard += 1

    if shares <= 0:
        return 0.0, ["预算不足一份"]

    # 单腿金额红线按"未取整的真实支出"判定: 取整会掩盖不足额的真实缺口
    leg_0 = shares * price_0
    leg_1 = shares * price_1
    if leg_0 < limits.min_leg_usd:
        reasons.append(f"leg0 金额 {leg_0:.4f} < {limits.min_leg_usd}")
    if leg_1 < limits.min_leg_usd:
        reasons.append(f"leg1 金额 {leg_1:.4f} < {limits.min_leg_usd}")
    if shares * pair > budget_usd + 1e-9:
        reasons.append(f"总成本 {shares * pair:.4f} 超预算 {budget_usd}")
    return (0.0 if reasons else shares), reasons


# ---------------------------------------------------------------------------
# neg-risk 事件组敞口记账
# ---------------------------------------------------------------------------

def group_add_usd(shares: float, price_0: float, price_1: float) -> float:
    """本笔订单对单一事件组的资金占用 = 份额 × 双腿总价。

    双腿是同一 outcome 的 YES/NO 互补腿, 共用同一个 condition_id, 因此整对
    都计入同一个事件组, 而不是两笔独立敞口。
    """
    return float(shares) * (float(price_0) + float(price_1))


def group_limit_ok(
    group_key: Optional[str],
    group_exposure_usd: float,
    group_add_usd: float,
    limits: Limits,
) -> bool:
    """单独问"这笔之后该事件组是否仍不超限"(供实时撮合前逐单校验)。"""
    if not group_key or not limits.enforce_group_cap:
        return True
    return (group_exposure_usd + group_add_usd) <= limits.max_exposure_per_neg_risk_group_usd + 1e-9
