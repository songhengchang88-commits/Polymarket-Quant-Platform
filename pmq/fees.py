"""成本模型: 手续费 + Gas。

Polymarket CLOB 的 taker 费按概率对称收取:
    fee = shares * (fee_rate_bps / 10000) * min(price, 1 - price)
(maker 端当前通常为 0, 但必须可从接口读取而非写死)

把成本显式建模, 才能在开仓前判断"锁定边际是否真的为正"。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    taker_fee_bps: float = 0.0
    gas_usd: float = 0.002

    def taker_fee(self, price: float, shares: float) -> float:
        p = min(max(float(price), 0.0), 1.0)
        return float(shares) * (self.taker_fee_bps / 10_000.0) * min(p, 1.0 - p)

    def pair_cost(self, price_0: float, price_1: float, shares: float) -> float:
        """买入对称份数 shares 的双腿总现金支出(含费与 gas)。"""
        return (
            shares * (price_0 + price_1)
            + self.taker_fee(price_0, shares)
            + self.taker_fee(price_1, shares)
            + self.gas_usd
        )

    def locked_edge(self, price_0: float, price_1: float, shares: float) -> float:
        """两腿都成交并持有到 1.00 结算时的净锁定边际。"""
        payout = shares * 1.0
        return payout - self.pair_cost(price_0, price_1, shares)
