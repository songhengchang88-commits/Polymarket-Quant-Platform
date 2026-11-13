"""
账户资金规模自适应量化策略调节器 (Adaptive Capital Strategy Engine)
===================================================================
根据实时链上资产余额，全自动、动态地优化量化策略的各项风控与收益参数:
- 资金规模动态分级 (MICRO / GROWTH / SCALE)
- 单笔仓位动态反推 (平衡 1.15U 单腿物理底线与百分比敞口)
- 并发持仓组数自适应 (资金小时集中不分兵，资金大时矩阵化并发)
- 盘口价格与利润阈值动态调整 (随资金厚度动态放宽或收紧过滤边界)
"""

from dataclasses import dataclass
from typing import Tuple, Dict, Any
import math

@dataclass
class CapitalStrategyConfig:
    tier: str                      # "MICRO" (<$15), "GROWTH" ($15~$100), "SCALE" (>$100)
    tier_name_zh: str              # 中文等级描述
    balance_usd: float             # 当前链上实时可用余额
    max_alloc_per_trade: float     # 单笔允许的最大对冲金额
    position_cap_pct: float        # 单笔占总资金上限百分比
    max_concurrent_bags: int       # 允许同时持有的对冲锁组数
    min_leg_price: float           # 允许发单的单腿最低单价
    max_leg_price: float           # 允许发单的单腿最高单价
    parity_threshold: float        # 负风险套利门槛 (Ask0 + Ask1 <= threshold)
    cash_reserve_usd: float        # 必须留存的流动性现金垫
    description: str               # 当前资金规模下的策略优化说明

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_name_zh": self.tier_name_zh,
            "balance_usd": self.balance_usd,
            "max_alloc_per_trade": self.max_alloc_per_trade,
            "position_cap_pct": self.position_cap_pct,
            "max_concurrent_bags": self.max_concurrent_bags,
            "min_leg_price": self.min_leg_price,
            "max_leg_price": self.max_leg_price,
            "parity_threshold": self.parity_threshold,
            "cash_reserve_usd": self.cash_reserve_usd,
            "description": self.description,
        }

class AdaptiveCapitalEngine:
    @staticmethod
    def evaluate(balance_usd: float) -> CapitalStrategyConfig:
        bal = max(0.0, float(balance_usd))

        # -------------------------------------------------------------
        # 1. 微型资金阶梯 (< $15.0 USDC，如当前阶段 ~$4.61U)
        # -------------------------------------------------------------
        if bal < 15.0:
            # 策略重点: 资金规模较小，严守单跑道 (max_concurrent_bags = 1)，绝不分兵
            # 为保证在满足单腿 $1.15 官方门槛的同时两腿顺利对冲，单笔预算充分释放 (留 0.25U 滑点/费率安全垫)
            reserve = 0.25 if bal >= 2.60 else 0.10
            alloc = round(max(0.0, bal - reserve), 2)
            cap_pct = round(alloc / max(bal, 1e-6), 2)

            # 自适应收紧单腿单价下限，确保低价腿最低份额 * 总成本 <= alloc (杜绝小资金买不起高份数)
            # 例如 alloc=4.36 时，min_p=0.26，低价边 1.15/0.26=4.4 份 * 0.98 = 4.31U <= 4.36U
            min_p = round(max(0.15, min(0.35, 1.15 / max(alloc, 2.30))), 2)
            max_p = round(1.0 - min_p, 2)

            return CapitalStrategyConfig(
                tier="MICRO",
                tier_name_zh="🛡️ 微型资金阶梯 (严控单跑道·全效自适应)",
                balance_usd=bal,
                max_alloc_per_trade=alloc,
                position_cap_pct=cap_pct,
                max_concurrent_bags=1,      # 小资金集中一进一出，严禁资金碎片化
                min_leg_price=min_p,        # 动态自适应价格区间，确保资金足额覆盖
                max_leg_price=max_p,
                parity_threshold=0.985,     # 严守高标准净利 (>= 1.5%)
                cash_reserve_usd=reserve,
                description=f"单跑道全效自适应 (单笔上限 {alloc:.2f}U)，价格边界自适应收紧至 [{min_p:.2f}~{max_p:.2f}]，确保满足单腿 $1.15 官方红线"
            )

        # -------------------------------------------------------------
        # 2. 成长资金阶梯 ($15.0 ~ $100.0 USDC，用户小额追加资金后自动激活)
        # -------------------------------------------------------------
        elif bal < 100.0:
            # 策略重点: 释放多跑道并发能力 (2~3 组对冲锁)，单笔提升，恒定留存 30% 现金底仓
            cap_pct = 0.35
            alloc = round(min(bal * cap_pct, 20.0), 2)
            reserve = round(bal * 0.30, 2)
            return CapitalStrategyConfig(
                tier="GROWTH",
                tier_name_zh="🚀 成长资金阶梯 (多跑道并发)",
                balance_usd=bal,
                max_alloc_per_trade=alloc,
                position_cap_pct=cap_pct,
                max_concurrent_bags=3,      # 开放 3 组并发对冲
                min_leg_price=0.04,         # 适度放宽盘口选择面
                max_leg_price=0.96,
                parity_threshold=0.990,     # 净利门槛平滑至 1.0%
                cash_reserve_usd=reserve,
                description="开启 3 组多标的并发套利，资金吞吐放大，30% 底层现金护城河"
            )

        # -------------------------------------------------------------
        # 3. 规模资金阶梯 (>= $100.0 USDC)
        # -------------------------------------------------------------
        else:
            # 策略重点: 矩阵化并发扫描，高频价差吞吐，大资金流水复利
            cap_pct = 0.20
            alloc = round(min(bal * cap_pct, 50.0), 2)
            reserve = round(bal * 0.25, 2)
            return CapitalStrategyConfig(
                tier="SCALE",
                tier_name_zh="💎 规模资金阶梯 (全网高频矩阵)",
                balance_usd=bal,
                max_alloc_per_trade=alloc,
                position_cap_pct=cap_pct,
                max_concurrent_bags=5,      # 开放 5 组全网高频并发
                min_leg_price=0.02,         # 覆盖全深度订单簿
                max_leg_price=0.98,
                parity_threshold=0.992,     # 捕获毫厘价差 (>= 0.8%)
                cash_reserve_usd=reserve,
                description="全网矩阵化高频对冲，多标的高流动性吞吐，大资金快速复利"
            )
