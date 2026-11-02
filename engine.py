import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import config
from polymarket import PolymarketClient
from arbitrage_scanner import ArbitrageScanner
from paper_trader import PaperTrader
from live_executor import LiveClobExecutor
from news_watcher import NewsWatcher

logger = logging.getLogger("quant_engine")
LATEST_MARKETS_CACHE = os.path.join(config.DATA_DIR, "latest_markets.json")

class QuantEngine:
    def __init__(self):
        self.client = PolymarketClient()
        self.scanner = ArbitrageScanner()
        self.trader = PaperTrader(initial_balance=config.INITIAL_BALANCE_USD)
        self.live_executor = LiveClobExecutor()
        self.news_watcher = NewsWatcher()

        self.execution_mode = config.EXECUTION_MODE  # 'paper' or 'live'
        self.is_running = True
        self.scan_interval = config.DEFAULT_SCAN_INTERVAL
        self.current_category = config.DEFAULT_CATEGORY

        self.monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 内存数据与缓存
        self.latest_scan_time = ""
        self.latest_markets_data: List[Dict] = []
        self.latest_live_trades: List[Dict] = []
        self.latest_avg_latency_ms = 450.0
        self.scan_count = 0
        self.total_opportunities_found = 0

        self._load_cached_markets()
        self.news_watcher.start()
        self.start_monitor()

    def _load_cached_markets(self):
        # 始终优先进行实时全网扫描，不加载静态陈旧数据
        self.latest_markets_data = []

    def _save_cached_markets(self, markets: List[Dict]):
        try:
            with open(LATEST_MARKETS_CACHE, "w", encoding="utf-8") as f:
                json.dump(markets, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def set_execution_mode(self, mode: str):
        if mode in ("paper", "live"):
            self.execution_mode = mode
            config.EXECUTION_MODE = mode
            logger.info(f"🔄 交易模式已切换至: {mode.upper()}")
            return True
        return False

    def start_monitor(self):
        with self._lock:
            self.is_running = True
            if self.monitor_thread is None or not self.monitor_thread.is_alive():
                self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
                self.monitor_thread.start()
                logger.info("全链条量化监控线程已启动")

    def stop_monitor(self):
        with self._lock:
            self.is_running = False
            logger.info("全链条量化监控线程已停止")

    def set_category(self, cat: str):
        self.current_category = cat
        self.scan_once()

    def _monitor_loop(self):
        while self.is_running:
            try:
                # 喂狗保持死人开关活跃
                if hasattr(self.live_executor, "deadman") and self.live_executor.deadman:
                    self.live_executor.deadman.heartbeat()
                self.scan_once()
            except Exception as e:
                logger.error(f"扫描轮询异常: {e}")
            time.sleep(self.scan_interval)

    def scan_once(self) -> Dict:
        start_time = time.time()
        
        # 1. 抓取全品类市场 (长尾体育, 选举, 加密)
        raw_markets = self.client.get_top_volume_markets(limit=config.SCAN_MARKET_LIMIT, category=self.current_category)
        if not raw_markets:
            return {"status": "error", "message": "无法获取活跃市场"}

        token_id_map = {}
        for m in raw_markets:
            tokens = m.get("clob_token_ids", [])
            if len(tokens) >= 2:
                token_id_map[tokens[0]] = (m, 0)
                token_id_map[tokens[1]] = (m, 1)

        token_ids = list(token_id_map.keys())
        books = self.client.get_bulk_orderbooks(token_ids)

        parsed_markets = []
        opps_in_this_scan = []
        latencies = []

        # 2. 逐个市场扫描
        for m in raw_markets:
            tokens = m.get("clob_token_ids", [])
            if len(tokens) < 2:
                continue

            ob_0 = books.get(tokens[0], {})
            ob_1 = books.get(tokens[1], {})

            if not ob_0 or not ob_1:
                continue

            if "latency_ms" in ob_0:
                latencies.append(ob_0["latency_ms"])
            if "latency_ms" in ob_1:
                latencies.append(ob_1["latency_ms"])

            # 评估四向机会
            parity_opp, reverse_opp, mm_opp = self.scanner.evaluate_market_pair(m, ob_0, ob_1)

            # 优先 Taker 负风险套利
            if parity_opp:
                opps_in_this_scan.append(parity_opp)
                # 纸面/实盘分流执行
                self.trader.execute_parity_arbitrage(parity_opp)
                if self.execution_mode == "live" and self.live_executor.is_ready:
                    tokens = m.get("clob_token_ids", [])
                    if len(tokens) >= 2:
                        # 小仓位调度: 每次投入 1.0~2.0U 极小仓位，多仓并发快进快出，绝不压重注
                        cur_bal = self.live_executor.get_realtime_balance()
                        if cur_bal >= 2 * config.MIN_ORDER_LEG_USD:
                            # [修正 2026-11-02] 单笔预算 = 余额 × POSITION_CAP_PCT,
                            # 具体份数由执行层按盘口反推, 不再用固定 2.6U 去撞 1.15U/腿 红线
                            live_alloc = round(cur_bal * config.POSITION_CAP_PCT, 2)
                            logger.info(f"⚡ [小仓位快进] 可用: {cur_bal:.2f}U | 单笔微投入: {live_alloc:.2f}U | 目标: {parity_opp.get('question', '')[:30]}")
                            self.live_executor.place_parity_order(
                                token_id_0=tokens[0],
                                token_id_1=tokens[1],
                                price_0=parity_opp["ask_0"],
                                price_1=parity_opp["ask_1"],
                                amount_usd=live_alloc,
                                question=parity_opp.get("question", ""),
                                market_id=str(m["id"])
                            )
                        else:
                            logger.info(f"⏳ [资金周转中] 当前可用 {cur_bal:.2f}U，等待微利平仓回收后立即出手下一次")
            # 其次，在没有 Taker 时且 Maker Edge 丰厚时，执行 Maker 做市挂单
            elif mm_opp and mm_opp.get("maker_profit_space_pct", 0) >= 1.5:
                if len(self.trader.active_positions) < config.MAX_CONCURRENT_BAGS:
                    self.trader.execute_maker_spread(mm_opp)

            ask_0 = ob_0.get("best_ask", 1.0)
            ask_1 = ob_1.get("best_ask", 1.0)
            bid_0 = ob_0.get("best_bid", 0.0)
            bid_1 = ob_1.get("best_bid", 0.0)
            pair_cost = ask_0 + ask_1 if (0 < ask_0 < 1.0 and 0 < ask_1 < 1.0) else 1.0

            parsed_markets.append({
                "id": m["id"],
                "question": m["question"],
                "slug": m.get("slug", ""),
                "volume": m["volume"],
                "category": m.get("category", "crypto"),
                "outcome_0": m["outcome_0"],
                "outcome_1": m["outcome_1"],
                "clob_token_ids": tokens,
                "best_bid_0": bid_0,
                "best_ask_0": ask_0,
                "best_bid_1": bid_1,
                "best_ask_1": ask_1,
                "pair_cost": round(pair_cost, 4),
                "taker_margin_pct": round((1.0 - pair_cost) * 100, 2),
                "has_maker_edge": mm_opp is not None,
                "maker_edge_pct": mm_opp.get("maker_profit_space_pct", 0.0) if mm_opp else 0.0,
                "depth_count": ob_0.get("depth_count", 0) + ob_1.get("depth_count", 0),
                "latency_ms": (ob_0.get("latency_ms", 0) + ob_1.get("latency_ms", 0)) / 2,
                "tag_badge": m.get("tag_badge", "GENERAL"),
                "is_today": m.get("is_today", False),
                "end_date": m.get("end_date", "")
            })

        # 3. 检查当前活跃持仓的均值回归离场与做市撮合
        self.trader.check_and_exit_positions(parsed_markets)
        if self.execution_mode == "live" and self.live_executor.live_orders:
            self._check_live_exits(parsed_markets)

        with self._lock:
            self.latest_markets_data = parsed_markets
            self.latest_scan_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.scan_count += 1
            self.total_opportunities_found += len(opps_in_this_scan)
            if latencies:
                self.latest_avg_latency_ms = sum(latencies) / len(latencies)

        if parsed_markets:
            self._save_cached_markets(parsed_markets)

        return {
            "status": "success",
            "markets_count": len(parsed_markets),
            "opportunities_count": len(opps_in_this_scan),
            "duration_ms": round((time.time() - start_time) * 1000, 1)
        }

    def _check_live_exits(self, parsed_markets: List[Dict]):
        """
        零滑点稳胜离场安全盾:
        1. 严禁向买卖价差 (Bid-Ask Spread) 割肉市价平仓！
        2. 仅当买盘双边出现反向溢价 (Bid0 + Bid1 >= 1.008) 且平仓确定拿到真金白银净利时才提前脱手；
        3. 其余情况坚决锁定持仓，持有直至比赛或决胜到期，由智能合约以法定精确 $1.000 USD 兜底兑付！
        """
        market_map = {str(m["id"]): m for m in parsed_markets}
        for m_id, rec in list(self.live_executor.live_orders.items()):
            m = market_map.get(str(m_id))
            if not m:
                continue

            bid_0 = m.get("best_bid_0", 0.0)
            bid_1 = m.get("best_bid_1", 0.0)
            if bid_0 <= 0 or bid_1 <= 0:
                continue

            shares = rec.get("shares", 0.0)
            invested = rec.get("invested_usd", 1.0)
            bid_sum = bid_0 + bid_1
            current_payout = shares * bid_sum
            net_profit_usd = current_payout - invested

            # 提前退出的唯一科学标准: 买盘总和 >= 1.008 且净收益 > 0
            if bid_sum >= config.REVERSE_ARB_THRESHOLD and net_profit_usd > 0.01:
                profit_pct = (net_profit_usd / invested) * 100.0
                logger.info(f"💎 [反向溢价结利] 市场 {m_id} 买盘总和 {bid_sum:.3f} >= {config.REVERSE_ARB_THRESHOLD}，双边无损平仓回收 {current_payout:.2f}U (+{profit_pct:.2f}%)")
                self.live_executor.close_parity_position(m_id, bid_0=bid_0, bid_1=bid_1)
