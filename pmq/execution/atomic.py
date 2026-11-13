"""原子双腿执行器。

这是把开源经验(尤其 poly-maker 的 ExecutionGateway / reconcile 思路)
落到本项目的最小内核:

- 双腿用 FOK 下单, 避免"一半成交"的单边裸持;
- 任一腿失败立刻撤单/平掉已成交部分, 并把失败原因写进账本;
- 盈亏只由成交回执推导, 绝不使用"模型预期利润";
- 所有异常都被收敛成 ExecutionResult, 不让主循环崩掉。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pmq import risk
from pmq.fees import CostModel

try:
    from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2, OrderType as OrderTypeV2
    OrderArgs = OrderArgsV2
    OrderType = OrderTypeV2
except Exception:
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
    except Exception:  # pragma: no cover
        OrderArgs = None

        class OrderType:  # type: ignore[no-redef]
            FOK = "FOK"
            FAK = "FAK"
            GTC = "GTC"


def make_order_args(token_id: str, price: float, size: float, side: str, fee_rate_bps: int = 0) -> Any:
    """根据实际安装的 SDK 动态生成兼容的 OrderArgs 对象，严格对齐 Polymarket CLOB 官方精度硬约束:
    - price: 最多 3 位小数 (0.001 tick)
    - size: 严格对齐，使 BUY 订单的 maker amount (price * size) 绝不超过 2 位小数，彻底消除 invalid amounts 400 报错！
    """
    import math
    if OrderArgs is None:
        raise RuntimeError("py_clob_client 未安装")
    safe_price = round(float(price), 3)
    safe_size = round(float(size), 4)

    # 官方铁律防御: 对于 BUY 订单，maker amount (price * size) 必须严格 <= 2 位小数
    if str(side).upper() == "BUY":
        raw_cost = round(safe_price * safe_size, 6)
        if round(raw_cost, 2) != raw_cost:
            # 向下修约至整份或纯美分无损份数
            int_s = math.floor(safe_size)
            if int_s > 0 and round(round(safe_price * int_s, 6), 2) == round(safe_price * int_s, 6):
                safe_size = float(int_s)
            else:
                cents = math.floor(raw_cost * 100) / 100.0
                safe_size = round(cents / safe_price, 4)

    try:
        # V2: OrderArgsV2(token_id=..., price=..., size=..., side=...)
        return OrderArgs(token_id=token_id, price=safe_price, size=safe_size, side=side)
    except TypeError:
        # V1: 包含 fee_rate_bps
        return OrderArgs(token_id=token_id, price=safe_price, size=safe_size, side=side, fee_rate_bps=fee_rate_bps)


EPS = 1e-9


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(float(price), 6)
    steps = round(float(price) / tick)
    return round(steps * tick, 6)


@dataclass
class ExecutionResult:
    status: str                      # LOCKED / COMPENSATED / UNHEDGED / ABORTED / REJECTED / ERROR
    filled_shares: float = 0.0
    cash_out: float = 0.0
    fees_usd: float = 0.0
    locked_edge_usd: float = 0.0
    price_0: float = 0.0
    price_1: float = 0.0
    order_ids: Dict[str, Optional[str]] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    unhedged_shares: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "LOCKED"

    @property
    def has_exposure(self) -> bool:
        return self.unhedged_shares > EPS


class AtomicPairExecutor:
    def __init__(self, client, journal=None, costs: Optional[CostModel] = None, limits: Optional[risk.Limits] = None):
        self.client = client
        self.journal = journal
        self.costs = costs or CostModel()
        self.limits = limits or risk.Limits()

    # ---------------- 基础设施 ----------------
    def _log(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if self.journal is not None:
            try:
                return self.journal.append(event)
            except Exception:
                pass
        return event

    def _buy_fok(self, token_id: str, price: float, size: float) -> Any:
        args = make_order_args(token_id=token_id, price=price, size=size, side="BUY")
        signed = self.client.create_order(args)
        return self.client.post_order(signed, OrderType.FOK)

    def _sell_fak(self, token_id: str, price: float, size: float) -> Any:
        args = make_order_args(token_id=token_id, price=price, size=size, side="SELL")
        signed = self.client.create_order(args)
        return self.client.post_order(signed, OrderType.FAK)

    def _parse_fill(self, resp: Any, requested: float) -> tuple:
        """从回执里解析真实成交量。返回 (filled, status, order_id)。"""
        if not isinstance(resp, dict):
            return 0.0, "unknown", None
        status = str(resp.get("status", "")).lower()
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        for key in ("size_matched", "matched_amount", "filled_size"):
            value = resp.get(key)
            if value in (None, "", []):
                continue
            try:
                return float(value), status, order_id
            except (TypeError, ValueError):
                continue
        if status in ("matched", "filled"):
            return float(requested), status, order_id

        # 终态主动查询对账兜底: 彻底消灭 status=delayed 假失败真成交漏洞
        if order_id and hasattr(self, "client") and hasattr(self.client, "get_order"):
            max_polls = 6 if status in ("delayed", "live", "pending", "") else 1
            for poll_idx in range(max_polls):
                if poll_idx > 0:
                    time.sleep(0.3)
                try:
                    od = self.client.get_order(order_id)
                    if isinstance(od, dict):
                        od_status = str(od.get("status", "")).lower()
                        for key in ("size_matched", "matched_amount", "filled_size"):
                            v = od.get(key)
                            if v not in (None, "", []):
                                f_val = float(v)
                                if f_val > EPS:
                                    return f_val, od_status, order_id
                        if od_status in ("matched", "filled"):
                            return float(requested), od_status, order_id
                        if od_status in ("canceled", "cancelled", "killed", "rejected"):
                            return 0.0, od_status, order_id
                except Exception:
                    pass

        return 0.0, status, order_id

    def _cancel_quiet(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        for name in ("cancel_orders", "cancel_order", "cancel"):
            fn = getattr(self.client, name, None)
            if fn is None:
                continue
            try:
                if name == "cancel_orders":
                    fn([order_id])
                else:
                    fn(order_id)
                return
            except Exception:
                continue

    def _panic_cancel_all(self) -> bool:
        """下单异常后的兜底: 交易所可能已收单, 只是回执丢了。

        这是实盘最危险的状态 —— 本地以为失败, 实际已挂单。不撤干净就会重复下单。
        """
        fn = getattr(self.client, "cancel_all", None)
        if fn is None:
            return False
        try:
            out = fn()
            self._log({"type": "PANIC_CANCEL", "result": str(out)[:300]})
            return True
        except Exception as exc:
            self._log({"type": "PANIC_CANCEL_FAILED", "error": str(exc)[:300]})
            return False

    def _unwind(self, token_id: str, shares: float, bid: float) -> bool:
        """把已成交的单腿按买一价 FAK 卖出, 尽量消除敞口。带 3 次重试与精度对齐保底。"""
        if shares <= EPS:
            return False
        
        # 强制将份额修约到 2 位小数，防止 invalid amounts 精度报错
        safe_shares = round(shares, 2)
        if safe_shares <= EPS:
            return False

        # 买一价兜底: 若传入的 bid 为 0，使用极小兜底价 0.01 强行平仓避险
        safe_bid = round(max(0.01, bid), 3)

        for attempt in range(3):
            try:
                # 尝试以 safe_bid 或稍微让利 1 个 tick 保证秒成交
                sell_price = round(max(0.01, safe_bid - attempt * 0.005), 3)
                resp = self._sell_fak(token_id, sell_price, safe_shares)
                filled, _, _ = self._parse_fill(resp, safe_shares)
                if filled > EPS:
                    self._log({"type": "UNWIND_SUCCESS", "token_id": token_id, "shares": filled, "attempt": attempt+1})
                    return True
            except Exception as e:
                self._log({"type": "UNWIND_RETRY_ERR", "attempt": attempt+1, "error": str(e)[:200]})
                time.sleep(0.3)

        return False

    # ---------------- 主流程 ----------------
    def execute(
        self,
        market_id: str,
        token_0: str,
        token_1: str,
        book_0: risk.Book,
        book_1: risk.Book,
        budget_usd: float,
        now: Optional[float] = None,
        exposure_usd: float = 0.0,
        daily_pnl: float = 0.0,
    ) -> ExecutionResult:
        now = time.time() if now is None else now
        try:
            return self._execute(market_id, token_0, token_1, book_0, book_1,
                                 budget_usd, now, exposure_usd, daily_pnl)
        except Exception as exc:  # 绝不让异常穿透到扫描循环
            result = ExecutionResult(status="ERROR", reasons=[f"{type(exc).__name__}: {exc}"])
            self._log({"type": "ERROR", "market_id": market_id, "error": str(exc)})
            return result

    def _execute(self, market_id, token_0, token_1, book_0, book_1,
                 budget_usd, now, exposure_usd, daily_pnl) -> ExecutionResult:

        gate = risk.check_pair(book_0, book_1, self.limits, now, exposure_usd, daily_pnl)
        if not gate.ok:
            self._log({"type": "REJECT", "market_id": market_id, "reasons": gate.reasons})
            return ExecutionResult(status="REJECTED", reasons=gate.reasons)

        p0 = round_to_tick(book_0.ask, book_0.tick)
        p1 = round_to_tick(book_1.ask, book_1.tick)
        if p0 + p1 > self.limits.max_pair_cost:
            reason = f"取整后配对成本 {p0 + p1:.4f} 超限"
            self._log({"type": "REJECT", "market_id": market_id, "reasons": [reason]})
            return ExecutionResult(status="REJECTED", reasons=[reason])

        shares, why = risk.max_shares_for_budget(budget_usd, p0, p1, self.limits)
        if shares <= 0:
            self._log({"type": "REJECT", "market_id": market_id, "reasons": why})
            return ExecutionResult(status="REJECTED", reasons=why)

        # ---- 第一腿 ----
        try:
            resp_0 = self._buy_fok(token_0, p0, shares)
        except Exception as exc:
            # 超时不等于失败: 交易所可能已经收单。必须全撤并标记 UNKNOWN。
            self._panic_cancel_all()
            self._log({"type": "UNKNOWN", "market_id": market_id, "leg": 0, "error": str(exc)})
            return ExecutionResult(status="UNKNOWN", reasons=[f"leg0 提交异常(可能已成交): {exc}"])

        filled_0, status_0, oid_0 = self._parse_fill(resp_0, shares)
        if filled_0 < shares - EPS:
            self._cancel_quiet(oid_0)
            # 撤单后二次深度对账: 防止交易所延迟撮合已成交而本地误当未成交放弃对冲
            time.sleep(0.2)
            try:
                if oid_0 and hasattr(self, "client") and hasattr(self.client, "get_order"):
                    post_od = self.client.get_order(oid_0)
                    if isinstance(post_od, dict):
                        p_st = str(post_od.get("status", "")).lower()
                        for k in ("size_matched", "matched_amount", "filled_size"):
                            v = post_od.get(k)
                            if v not in (None, "", []):
                                filled_0 = max(filled_0, float(v))
                        if p_st in ("matched", "filled"):
                            filled_0 = shares
            except Exception:
                pass

            if filled_0 < shares - EPS:
                reason = f"leg0 未足额成交 {filled_0}/{shares} (status={status_0})"
                if filled_0 <= EPS:
                    self._log({"type": "ABORT", "market_id": market_id, "reasons": [reason]})
                    return ExecutionResult(status="ABORTED", reasons=[reason],
                                           order_ids={"leg0": oid_0})
            # 部分成交 -> 立即尝试平掉, 避免裸持
            hedged = self._unwind(token_0, filled_0, book_0.bid)
            self._log({"type": "PARTIAL", "market_id": market_id, "leg": 0,
                       "filled": filled_0, "unwound": hedged, "reasons": [reason]})
            return ExecutionResult(
                status="COMPENSATED" if hedged else "UNHEDGED",
                filled_shares=filled_0,
                unhedged_shares=0.0 if hedged else filled_0,
                order_ids={"leg0": oid_0},
                reasons=[reason] + (["已按买一价平掉"] if hedged else ["平仓失败, 存在裸敞口"]),
            )

        # ---- 第二腿 ----
        try:
            resp_1 = self._buy_fok(token_1, p1, shares)
        except Exception as exc:
            hedged = self._unwind(token_0, filled_0, book_0.bid)
            self._log({"type": "LEG2_FAIL", "market_id": market_id, "error": str(exc),
                       "unwound": hedged})
            return ExecutionResult(
                status="COMPENSATED" if hedged else "UNHEDGED",
                filled_shares=filled_0,
                unhedged_shares=0.0 if hedged else filled_0,
                order_ids={"leg0": oid_0},
                reasons=[f"leg1 异常: {exc}"],
            )

        filled_1, status_1, oid_1 = self._parse_fill(resp_1, shares)
        if filled_1 < shares - EPS:
            self._cancel_quiet(oid_1)
            unhedged = max(0.0, filled_0 - filled_1)
            hedged = self._unwind(token_0, unhedged, book_0.bid) if unhedged > EPS else True
            reason = f"leg1 未足额成交 {filled_1}/{shares} (status={status_1})"
            self._log({"type": "IMBALANCE", "market_id": market_id,
                       "filled_0": filled_0, "filled_1": filled_1, "unwound": hedged})
            return ExecutionResult(
                status="COMPENSATED" if hedged else "UNHEDGED",
                filled_shares=min(filled_0, filled_1),
                unhedged_shares=0.0 if hedged else unhedged,
                order_ids={"leg0": oid_0, "leg1": oid_1},
                reasons=[reason],
            )

        # ---- 双腿成交: 用真实成交价核算 ----
        matched = min(filled_0, filled_1)
        cash_out = matched * (p0 + p1)
        fees = (self.costs.taker_fee(p0, matched)
                + self.costs.taker_fee(p1, matched)
                + self.costs.gas_usd)
        edge = matched * 1.0 - cash_out - fees

        result = ExecutionResult(
            status="LOCKED",
            filled_shares=matched,
            cash_out=round(cash_out, 6),
            fees_usd=round(fees, 6),
            locked_edge_usd=round(edge, 6),
            price_0=p0,
            price_1=p1,
            order_ids={"leg0": oid_0, "leg1": oid_1},
        )
        self._log({
            "type": "FILL_LOCKED",
            "market_id": market_id,
            "shares": matched,
            "price_0": p0,
            "price_1": p1,
            "cash_out": result.cash_out,
            "fees_usd": result.fees_usd,
            "locked_edge_usd": result.locked_edge_usd,
            "order_ids": result.order_ids,
        })
        return result
