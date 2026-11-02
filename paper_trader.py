import json
import logging
import os
import time
from typing import Dict, List, Optional
import config

logger = logging.getLogger("paper_trader")

class PaperTrader:
    """
    40U 专研微资金全链条模拟交易与仓位生命周期管理器：
    1. 严格锁定 40.0 USDC.e 初始本金与 20.0U 双仓槽位;
    2. 支持 Taker 负风险买入套利 + Maker 双边做市吃差价;
    3. 自动化均值回归平仓机制 (Mean Reversion Exit): 
       盘口价格修复至 0.995U 时自动平仓，释放 20U 资金继续轮动，实现高频资金周转！
    4. 扣除 Polygon 链上真实预估 Gas (~$0.005)。
    """
    def __init__(self, initial_balance: float = config.INITIAL_BALANCE_USD):
        self.initial_balance = initial_balance
        self.cash = initial_balance
        self.total_profit = 0.0
        self.trades_count = 0
        self.successful_trades = 0
        self.failed_trades = 0
        
        # 活跃套利持仓袋: market_id -> dict
        self.active_positions: Dict[str, Dict] = {}
        self.load_state()

    def load_state(self):
        if os.path.exists(config.STATS_FILE):
            try:
                with open(config.STATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.cash = data.get("current_cash", self.initial_balance)
                    self.total_profit = data.get("total_profit", 0.0)
                    self.trades_count = data.get("trades_count", 0)
                    self.successful_trades = data.get("successful_trades", 0)
                    self.failed_trades = data.get("failed_trades", 0)
                    self.active_positions = data.get("active_positions", {})
            except Exception as e:
                logger.warning(f"读取状态失败: {e}")

    def save_state(self):
        invested_in_bags = sum(p["invested_usd"] for p in self.active_positions.values())
        total_equity = self.cash + invested_in_bags
        data = {
            "initial_balance": self.initial_balance,
            "current_cash": round(self.cash, 4),
            "invested_in_bags": round(invested_in_bags, 4),
            "total_equity": round(total_equity, 4),
            "total_profit": round(self.total_profit, 4),
            "profit_pct": round(((total_equity - self.initial_balance) / self.initial_balance) * 100, 2),
            "trades_count": self.trades_count,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "win_rate_pct": round((self.successful_trades / max(1, self.trades_count)) * 100, 1),
            "active_positions_count": len(self.active_positions),
            "active_positions": self.active_positions,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(config.STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def reset_account(self):
        """一键重置为纯净的 40U 账户"""
        self.cash = self.initial_balance
        self.total_profit = 0.0
        self.trades_count = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self.active_positions.clear()
        if os.path.exists(config.TRADES_FILE):
            try:
                os.remove(config.TRADES_FILE)
            except Exception:
                pass
        self.save_state()

    def execute_parity_arbitrage(self, opp: Dict) -> Optional[Dict]:
        """执行 Taker 负风险配对套利 (20U 槽位)"""
        market_id = opp["market_id"]
        if market_id in self.active_positions:
            return None

        if len(self.active_positions) >= config.MAX_CONCURRENT_BAGS:
            return None

        alloc_usd = min(config.ALLOC_PER_TRADE_USD, self.cash * config.POSITION_CAP_PCT)
        if alloc_usd <= 2 * config.MIN_ORDER_LEG_USD:
            return None

        pair_cost = opp["pair_cost"]
        shares = alloc_usd / pair_cost
        expected_payout = shares * 1.00
        gas_cost = config.ESTIMATED_GAS_FEE_USD
        expected_profit = (expected_payout - alloc_usd) - gas_cost

        if expected_profit <= 0:
            return None

        # [新增 2026-11-02] 与实盘同一把尺子: 单腿价值必须 >= MIN_ORDER_LEG_USD,
        # 否则纸面统计无法与实盘比较(旧数据里 0.03/0.955 这类盘口在实盘会被直接拒单)。
        ask_0 = float(opp.get("ask_0", 0.0) or 0.0)
        ask_1 = float(opp.get("ask_1", 0.0) or 0.0)
        if ask_0 > 0 and ask_1 > 0:
            if (shares * ask_0 < config.MIN_ORDER_LEG_USD) or (shares * ask_1 < config.MIN_ORDER_LEG_USD):
                logger.info("🛑 [纸面拒绝] 单腿价值不足 MIN_ORDER_LEG_USD，不建仓")
                return None

        self.cash -= alloc_usd
        pos_id = f"pos-{int(time.time()*1000)}"
        position = {
            "id": pos_id,
            "market_id": market_id,
            "question": opp["question"],
            "pair_cost": pair_cost,
            "shares": round(shares, 2),
            "invested_usd": round(alloc_usd, 2),
            "expected_profit_usd": round(expected_profit, 2),
            "profit_pct": round((expected_profit / alloc_usd) * 100, 2),
            "entry_time": time.strftime("%H:%M:%S"),
            "entry_timestamp": time.time(),
            "status": "HOLDING_LOCK",
            "type": "PARITY_ARBITRAGE"
        }
        self.active_positions[market_id] = position

        self._append_trade_log({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "OPEN_BAG",
            "type": "PARITY_ARBITRAGE",
            "question": opp["question"],
            "cost_usd": round(alloc_usd, 2),
            "pair_cost": round(pair_cost, 4),
            "expected_profit_usd": round(expected_profit, 2),
            "balance_after": round(self.cash, 4)
        })
        self.save_state()
        logger.info(f"🎯 [建仓成功] Taker 负风险套利袋: {opp['question'][:30]} | 投入 {alloc_usd}U | 锁定利润 +{expected_profit:.2f}U")
        return position

    def execute_maker_spread(self, mm_opp: Dict) -> Optional[Dict]:
        """执行 Maker 双边做市吃差价 (20U 槽位)"""
        market_id = mm_opp["market_id"]
        if market_id in self.active_positions or len(self.active_positions) >= config.MAX_CONCURRENT_BAGS:
            return None

        alloc_usd = min(config.ALLOC_PER_TRADE_USD, self.cash * config.POSITION_CAP_PCT)
        if alloc_usd <= 2 * config.MIN_ORDER_LEG_USD:
            return None

        # [修正 2026-11-02] 旧逻辑用 alloc*edge% 凭空算利润, edge 来自 (ask-bid)/bid 的失真值。
        # 现改为按双边买价之和反推对称份数, 利润 = N*(1 - 组合买价) - gas, 只有真实吃满双边才成立。
        combined = float(mm_opp.get("combined_bid_cost", 0.0) or 0.0)
        if not (0.0 < combined < 1.0):
            return None
        shares = round(alloc_usd / combined, 2)
        if shares <= 0:
            return None
        expected_profit = round(shares * (1.0 - combined) - config.ESTIMATED_GAS_FEE_USD, 2)
        if expected_profit <= 0:
            return None
        edge_pct = round(expected_profit / max(alloc_usd, 0.01) * 100, 2)

        self.cash -= alloc_usd
        pos_id = f"maker-{int(time.time()*1000)}"
        position = {
            "id": pos_id,
            "market_id": market_id,
            "question": mm_opp["question"],
            "pair_cost": combined,
            "shares": shares,
            "invested_usd": round(alloc_usd, 2),
            "expected_profit_usd": round(expected_profit, 2),
            "profit_pct": round(edge_pct, 2),
            "entry_time": time.strftime("%H:%M:%S"),
            "entry_timestamp": time.time(),
            "status": "MAKER_SPREAD_ACTIVE",
            "type": "MAKER_SPREAD"
        }
        self.active_positions[market_id] = position

        self._append_trade_log({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "OPEN_MAKER",
            "type": "MAKER_SPREAD",
            "question": mm_opp["question"],
            "cost_usd": round(alloc_usd, 2),
            "pair_cost": position["pair_cost"],
            "expected_profit_usd": round(expected_profit, 2),
            "balance_after": round(self.cash, 4)
        })
        self.save_state()
        logger.info(f"💎 [做市挂单] 双边做市买卖单: {mm_opp['question'][:30]} | 投入 {alloc_usd}U | 捕获差价 +{edge_pct:.1f}%")
        return position

    def check_and_exit_positions(self, latest_markets: List[Dict]):
        """均值回归自动平仓"""
        market_map = {m["id"]: m for m in latest_markets}
        exited_keys = []
        now = time.time()

        for m_id, pos in list(self.active_positions.items()):
            m = market_map.get(m_id)
            should_exit = False
            exit_reason = ""
            realized_profit = 0.0
            # [修复 2026-11-02] 旧代码直接引用从未定义的 elapsed, 必抛 NameError,
            # 导致整个平仓路径不可达(仓位永久卡死)。此处补齐定义。
            elapsed = now - float(pos.get("entry_timestamp", now))

            if pos["type"] == "PARITY_ARBITRAGE":
                if m:
                    bid_sum = m.get("best_bid_0", 0.0) + m.get("best_bid_1", 0.0)
                    if bid_sum >= config.REVERSE_ARB_THRESHOLD:
                        # 按真实可回收现金结算, 不再直接使用建仓时的模型值
                        proceeds = pos["shares"] * bid_sum
                        realized_profit = round(
                            proceeds - pos["invested_usd"] - config.ESTIMATED_GAS_FEE_USD, 2
                        )
                        should_exit = True
                        exit_reason = f"双边反向溢价结利 (Bid和: {bid_sum:.3f})"
                    elif m.get("is_today") and elapsed >= 3600:
                        realized_profit = round(pos["expected_profit_usd"], 2)
                        should_exit = True
                        exit_reason = "到期 1.000U 承兑结算"
            elif pos["type"] == "MAKER_SPREAD":
                # 旧逻辑: 只要过了 120 秒就当作"撮合成功", 把模型利润记为已实现。
                # 新逻辑: 必须有真实的双边买盘回升、且平掉双边后现金流为正, 才允许退出。
                if m:
                    bid_sum = m.get("best_bid_0", 0.0) + m.get("best_bid_1", 0.0)
                    proceeds = pos["shares"] * bid_sum
                    net = round(
                        proceeds - pos["invested_usd"] - config.ESTIMATED_GAS_FEE_USD, 2
                    )
                    if net > 0:
                        realized_profit = net
                        should_exit = True
                        exit_reason = f"做市双边回补成交 (Bid和: {bid_sum:.3f})"
                if (not should_exit) and elapsed >= config.MAKER_FILL_TIMEOUT_SEC:
                    realized_profit = -config.ESTIMATED_GAS_FEE_USD
                    should_exit = True
                    exit_reason = "做市超时未成交, 撤单退出"

            if should_exit:
                self.cash += (pos["invested_usd"] + realized_profit)
                self.total_profit += realized_profit
                self.trades_count += 1
                # [修复] 亏损单必须进入 failed_trades, 否则 win_rate 结构性恒为 100%
                if realized_profit >= 0:
                    self.successful_trades += 1
                else:
                    self.failed_trades += 1
                exited_keys.append(m_id)

                self._append_trade_log({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": "CLOSE_BAG",
                    "type": pos["type"],
                    "question": pos["question"],
                    "invested_usd": pos["invested_usd"],
                    "realized_profit_usd": round(realized_profit, 2),
                    "profit_pct": pos["profit_pct"],
                    "reason": exit_reason,
                    "balance_after": round(self.cash, 4)
                })
                logger.info(f"💰 [平仓结算] 释放本金 {pos['invested_usd']}U，净盈亏 {realized_profit:+.2f}U，当前可用现金: {self.cash:.2f}U")

        for k in exited_keys:
            del self.active_positions[k]

        if exited_keys:
            self.save_state()

    def close_position_manually(self, market_id: str) -> bool:
        """手动一键市价平仓"""
        if market_id in self.active_positions:
            pos = self.active_positions.pop(market_id)
            # [修正 2026-11-02] 旧实现按 expected_profit × 0.9 凭空产生"盈利"。
            # 无实时盘口可依时, 按成本价离场, 只损失 gas, 并如实计入失败笔数。
            profit = -config.ESTIMATED_GAS_FEE_USD
            self.cash += (pos["invested_usd"] + profit)
            self.total_profit += profit
            self.trades_count += 1
            self.failed_trades += 1
            self._append_trade_log({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": "MANUAL_CLOSE",
                "question": pos["question"],
                "invested_usd": pos["invested_usd"],
                "realized_profit_usd": round(profit, 2),
                "reason": "手动市价平仓释放",
                "balance_after": round(self.cash, 4)
            })
            self.save_state()
            return True
        return False

    def _append_trade_log(self, record: Dict):
        try:
            with open(config.TRADES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def get_summary(self) -> Dict:
        invested = sum(p["invested_usd"] for p in self.active_positions.values())
        equity = self.cash + invested
        return {
            "initial_balance": self.initial_balance,
            "current_cash": round(self.cash, 2),
            "invested_in_bags": round(invested, 2),
            "total_equity": round(equity, 2),
            "total_profit": round(self.total_profit, 2),
            "profit_pct": round(((equity - self.initial_balance) / self.initial_balance) * 100, 2),
            "trades_count": self.trades_count,
            "successful_trades": self.successful_trades,
            "win_rate_pct": round((self.successful_trades / max(1, self.trades_count)) * 100, 1),
            "active_positions_count": len(self.active_positions),
            "active_bags": list(self.active_positions.values())
        }

    def get_trades(self, limit: int = 30) -> List[Dict]:
        if not os.path.exists(config.TRADES_FILE):
            return []
        trades = []
        try:
            with open(config.TRADES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        trades.append(json.loads(line))
        except Exception:
            pass
        return trades[::-1][:limit]
