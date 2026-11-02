import json
import logging
import time
from typing import Dict, List, Optional, Tuple
import config

logger = logging.getLogger("arbitrage_scanner")

class ArbitrageScanner:
    """
    全链条四向套利与做市扫描引擎：
    1. 负风险配对套利 (Taker Buy: Ask0 + Ask1 < 0.985)
    2. 反向双边溢价清算 (Taker Sell: Bid0 + Bid1 > 1.015)
    3. 双边做市负溢价与高 Spread 捕获 (Maker Spread: 捕捉买卖价差 > 3.5%)
    4. 突发事件新闻不对称联动
    """
    def __init__(self):
        self.parity_threshold = config.PARITY_ARBITRAGE_THRESHOLD
        self.reverse_threshold = config.REVERSE_ARB_THRESHOLD
        self.min_spread_pct = config.MAKER_SPREAD_MIN_PCT

    def set_thresholds(self, parity_th: float = 0.985, reverse_th: float = 1.015):
        self.parity_threshold = parity_th
        self.reverse_threshold = reverse_th

    def evaluate_market_pair(self, market: Dict, ob_0: Dict, ob_1: Dict) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict]]:
        """
        全面深度评估一个二元市场的四向机会：
        :return: (parity_opp, reverse_opp, mm_opp)
        """
        ask_0 = ob_0.get("best_ask", 1.0)
        ask_0_size = ob_0.get("best_ask_size", 0.0)
        bid_0 = ob_0.get("best_bid", 0.0)
        bid_0_size = ob_0.get("best_bid_size", 0.0)

        ask_1 = ob_1.get("best_ask", 1.0)
        ask_1_size = ob_1.get("best_ask_size", 0.0)
        bid_1 = ob_1.get("best_bid", 0.0)
        bid_1_size = ob_1.get("best_bid_size", 0.0)

        parity_opp = None
        reverse_opp = None
        mm_opp = None

        # ---------------------------------------------------------------------
        # 1. 负风险配对买入套利 (Taker): Ask0 + Ask1 <= parity_threshold (例: 0.985)
        # ---------------------------------------------------------------------
        if 0 < ask_0 < 1.0 and 0 < ask_1 < 1.0:
            pair_cost = ask_0 + ask_1
            if pair_cost <= self.parity_threshold:
                max_shares = min(ask_0_size, ask_1_size)
                gross_profit_per_share = 1.00 - pair_cost
                total_gross_profit = max_shares * gross_profit_per_share
                
                parity_opp = {
                    "type": "PARITY_ARBITRAGE",
                    "direction": "BUY_PAIR_LOCK",
                    "market_id": market.get("id"),
                    "question": market.get("question"),
                    "slug": market.get("slug"),
                    "ask_0": ask_0,
                    "ask_1": ask_1,
                    "pair_cost": round(pair_cost, 4),
                    "profit_margin_pct": round((gross_profit_per_share / pair_cost) * 100, 2),
                    "max_executable_shares": round(max_shares, 2),
                    "potential_gross_profit": round(total_gross_profit, 2),
                    "latency_ms": round((ob_0.get("latency_ms", 0) + ob_1.get("latency_ms", 0)) / 2, 1),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                self._record_opportunity(parity_opp)

        # ---------------------------------------------------------------------
        # 2. 反向双边溢价清算 (Taker Sell): Bid0 + Bid1 >= reverse_threshold (例: 1.015)
        # ---------------------------------------------------------------------
        if bid_0 > 0 and bid_1 > 0:
            bid_sum = bid_0 + bid_1
            if bid_sum >= self.reverse_threshold:
                reverse_opp = {
                    "type": "REVERSE_PREMIUM",
                    "direction": "SELL_BOTH_EXIT",
                    "market_id": market.get("id"),
                    "question": market.get("question"),
                    "slug": market.get("slug"),
                    "bid_0": bid_0,
                    "bid_1": bid_1,
                    "bid_sum": round(bid_sum, 4),
                    "premium_margin_pct": round((bid_sum - 1.0) * 100, 2),
                    "max_sellable_shares": min(bid_0_size, bid_1_size),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                self._record_opportunity(reverse_opp)

        # ---------------------------------------------------------------------
        # 3. 双边做市买卖价差收割 (Maker): 捕捉高 Spread 盘口
        # ---------------------------------------------------------------------
        if bid_0 > 0 and ask_0 > 0 and bid_1 > 0 and ask_1 > 0:
            # [修正 2026-11-02] 旧实现用 (ask-bid)/bid 计算相对价差: bid=0.001 时 0.1 美分的
            # 真实价差会被放大成 100%, 双腿取均值再乘 0.8 正好得到数据里反复出现的 40.04%,
            # 并据此凭空记入利润。现改为以 1.00 USD 为基准的绝对价差, 并过滤极值/交叉/真空盘口。
            spread_0 = ask_0 - bid_0
            spread_1 = ask_1 - bid_1
            avg_spread_abs = (spread_0 + spread_1) / 2
            avg_spread_pct = avg_spread_abs * 100.0

            EXTREME_LOW, EXTREME_HIGH = 0.02, 0.98
            is_extreme = (
                bid_0 < EXTREME_LOW or ask_0 > EXTREME_HIGH
                or bid_1 < EXTREME_LOW or ask_1 > EXTREME_HIGH
            )
            # 双边买价之和 >= 1.0 => 盘口交叉或数据陈旧, 不是可收割的做市空间
            crossed = (bid_0 + bid_1) >= 1.0
            # 单边价差 > 5 美分 => 流动性真空, 挂单成交概率极低, 不是稳定的 edge
            liquidity_vacuum = avg_spread_abs > 0.05

            if (not is_extreme) and (not crossed) and (not liquidity_vacuum) and avg_spread_pct >= self.min_spread_pct:
                mm_opp = {
                    "type": "MAKER_SPREAD",
                    "market_id": market.get("id"),
                    "question": market.get("question"),
                    "bid_0": bid_0,
                    "ask_0": ask_0,
                    "bid_1": bid_1,
                    "ask_1": ask_1,
                    "avg_spread_pct": round(avg_spread_pct, 2),
                    # 只假设捕获一半价差, 且必须双边真实成交; 不再用 0.8 的乐观系数
                    "maker_profit_space_pct": round(
                        avg_spread_pct * config.PAPER_MAKER_EDGE_CONFIDENCE, 2
                    ),
                    "combined_bid_cost": round(bid_0 + bid_1, 4),
                    "fill_assumption": "REQUIRES_TWO_SIDED_FILL"
                }

        # 恪守稳胜铁律：严禁任何单边方向性猜跌涨下注，仅允许 100% 数学无风险配对套利与双边溢价高抛
        return parity_opp, reverse_opp, mm_opp

    def _record_opportunity(self, opp: Dict):
        try:
            with open(config.OPPORTUNITIES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(opp, ensure_ascii=False) + "\n")
        except Exception:
            pass
