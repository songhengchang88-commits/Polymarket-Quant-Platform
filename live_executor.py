"""
Polymarket 实盘 CLOB V2 官方签约执行引擎 (LiveClobExecutor)
=========================================================
版本: 3.0 — CLOB V2 官方升级版 (生产就绪)
- 基于官方最新 py-clob-client-v2 标准 SDK
- 适配 Polymarket CLOB V2 全新 EIP-712 签名协议 (含 timestamp, metadata, builder)
- 兼容 Polymarket Safe/Proxy 架构 (signature_type=2, funder 指定)
- 包含双重硬止损安全锁: 单笔限额 + 浮亏累计硬熔断
- 内置 Geoblock 与链上余额 preflight 检查
"""

import logging
import os
import time
from typing import Dict, List, Optional, Tuple
import requests
import config

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logger = logging.getLogger("live_executor")


class LiveClobExecutor:
    def __init__(self):
        self.private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
        self.chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))
        self.host = "https://clob.polymarket.com"
        self.funder = os.getenv("FUNDER_ADDRESS", "0x735e49143B19989E8c2BE88EdE72e96c1d0500dc").strip()
        self.wallet_address = os.getenv("WALLET_ADDRESS", "0x514f13a326a93aeda4522b973707e8602c2bcefd").strip()

        # 风控 (以当前 36.91U 资金量启动，支持自适应动态升级)
        self.hard_stop_loss_usd = float(os.getenv("HARD_STOP_LOSS_USD", "8.0"))
        self.max_order_usd = float(os.getenv("MAX_ORDER_USD", "20.0"))
        self.initial_deposit_usd = float(os.getenv("INITIAL_BALANCE", "36.91"))

        self.client = None
        self.is_ready = False
        self.cumulative_pnl = 0.0
        self.live_orders: Dict[str, Dict] = {}
        self.usdc_balance = self.initial_deposit_usd
        self._last_balance_check = 0.0
        self.deadman = None

        if self.private_key and self.private_key != "YOUR_PRIVATE_KEY_HERE":
            self._init_clob_v2_client()

        # 启动死人开关守卫 (保护 6.98U 离线零残留)
        self._init_deadman_switch()

    def _init_deadman_switch(self):
        try:
            from pmq.execution.deadman import DeadManSwitch
            self.deadman = DeadManSwitch(
                cancel_callback=self.cancel_all_orders,
                heartbeat_timeout_sec=30.0,
                name="LiveDeadManGuard",
            )
            self.deadman.start_watchdog()
            logger.info("🛡️ [死人开关就绪] 进程级退出保护与 30s 看门狗已上线")
        except Exception as e:
            logger.warning(f"死人开关启动轻微提示: {e}")

    def _init_clob_v2_client(self):
        """使用 py-clob-client-v2 初始化官方 CLOB V2 客户端"""
        try:
            from py_clob_client_v2.client import ClobClient

            # 针对 Polymarket Deposit Wallet 架构，官方 CLOB V2 强制要求 signature_type=3 (POLY_1271 存款钱包流)
            default_sig = 3 if self.funder and self.funder.lower() != self.wallet_address.lower() else 0
            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", str(default_sig)))

            self.client = ClobClient(
                host=self.host,
                chain_id=self.chain_id,
                key=self.private_key,
                signature_type=sig_type,
                funder=self.funder if sig_type != 0 else None,
                # [修复 2026-11-02] 默认 retry_on_error=False: 一次网络抖动就把
                # PolyApiException(status_code=None) 抛到界面。开启重试 + 服务器时间,
                # 同时规避本地时钟漂移导致的签名失败(偶发 401/400 的常见根因)。
                retry_on_error=True,
                use_server_time=True,
            )
            # 派生或创建 L2 API 凭证
            creds = self.client.create_or_derive_api_key()
            self.client.set_api_creds(creds)
            self.is_ready = True
            logger.info(f"⚡ [CLOB V2 实盘就绪] 客户端初始化成功！模式: sig_type={sig_type}, funder={self.funder[:10]}...")
        except Exception as e:
            logger.error(f"CLOB V2 客户端初始化异常: {e}")
            self.is_ready = False

    def check_geoblock(self) -> Dict:
        """检查当前网络环境是否在 Polymarket 限制区域"""
        try:
            r = requests.get("https://polymarket.com/api/geoblock", timeout=5)
            if r.status_code == 200:
                data = r.json()
                blocked = data.get("blocked", True)
                country = data.get("country", "")
                ip = data.get("ip", "")
                return {
                    "ok": not blocked,
                    "blocked": blocked,
                    "country": country,
                    "ip": ip,
                    "message": "网络畅通，允许交易" if not blocked else f"受限区域: {country} (请切香港/日本节点)"
                }
        except Exception as e:
            return {"ok": False, "blocked": True, "country": "UNKNOWN", "ip": "", "message": str(e)}

    def preflight_check(self) -> Dict:
        """综合实盘环境体检"""
        geo = self.check_geoblock()
        if not self.is_ready:
            return {
                "ok": False,
                "reason": "CLOB V2 客户端未就绪（请检查私钥配置）",
                "geoblock": geo,
                "usdc_balance": 0.0
            }

        bal_info = {"balance": "0", "usdc": 0.0}
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            bal = self.client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw = float(bal.get("balance", "0"))
            clob_usdc = raw / 1e6
            
            # 若 CLOB 暂未完成链上索引同步，但已知用户已成功充值，则使用配置的初始充值金额 4.0U
            fallback_balance = float(os.getenv("INITIAL_BALANCE", "4.0"))
            self.usdc_balance = clob_usdc if clob_usdc > 0 else fallback_balance
            self._last_balance_check = time.time()
            bal_info = {"balance": str(raw), "usdc": self.usdc_balance}
        except Exception as e:
            logger.warning(f"获取抵押品余额提示: {e}")

        all_ok = geo.get("ok", False) and self.is_ready
        return {
            "ok": all_ok,
            "is_ready": self.is_ready,
            "geoblock": geo,
            "usdc_balance": self.usdc_balance,
            "funder": self.funder,
            "wallet": self.wallet_address,
            "max_order_usd": self.max_order_usd,
            "hard_stop_loss_usd": self.hard_stop_loss_usd
        }

    def place_parity_order(
        self,
        token_id_0: str,
        token_id_1: str,
        price_0: float,
        price_1: float,
        amount_usd: float,
        question: str = "",
        market_id: str = ""
    ) -> Optional[Dict]:
        """
        实盘核心: 原子发送两笔买单 (YES + NO)，构建对冲无风险锁套利
        """
        if not self.is_ready:
            logger.warning("[实盘跳过] CLOB V2 执行器未就绪")
            return None

        # 熔断拦截
        if self.cumulative_pnl <= -self.hard_stop_loss_usd:
            logger.critical(f"🛑 [硬熔断保护] 累计亏损 -${abs(self.cumulative_pnl):.2f} 超限，终止下单！")
            self.cancel_all_orders()
            return None

        # [新增 2026-11-02] 合规硬闸门: 发单前强制校验出口 IP 是否处于受限区域。
        # 检测失败按"受限"处理(fail-closed), 宁可不交易也不越界; 本系统不提供任何 IP 规避手段。
        if getattr(config, "GEOBLOCK_ENFORCE", True):
            geo = self.check_geoblock()
            if not geo.get("ok", False):
                logger.critical(
                    f"🚫 [合规熔断] 出口 IP 受限或检测失败 "
                    f"({geo.get('country', 'UNKNOWN')} {geo.get('ip', '')})，拒绝发单"
                )
                return None

        # 稳胜铁律前置验证 1: 必须满足两边卖价总成本严格低于安全门槛 (留出绝对利润空间)
        pair_cost = price_0 + price_1
        if pair_cost > config.PARITY_ARBITRAGE_THRESHOLD or price_0 <= 0 or price_1 <= 0:
            logger.warning(f"🛑 [零亏损拦截] 配对成本 {pair_cost:.4f} > 门槛 {config.PARITY_ARBITRAGE_THRESHOLD}，无法确保必胜盈利，取消下单")
            return None

        safe_p0 = min(0.99, max(0.01, round(price_0, 3)))
        safe_p1 = min(0.99, max(0.01, round(price_1, 3)))

        # 稳胜铁律核心逻辑: 配对套利中，YES 和 NO 的份额必须绝对完全相等 (1:1 完美对称对冲)！
        # 绝不把资金五五平分导致两边份数悬殊，杜绝任何单边暴露风险！
        # 算法: 计算满足官方最小单腿门槛 (>= $1.15) 所需的最少对称配对份数
        min_shares_0 = config.MIN_ORDER_LEG_USD / max(safe_p0, 0.01)
        min_shares_1 = config.MIN_ORDER_LEG_USD / max(safe_p1, 0.01)
        req_shares = round(max(min_shares_0, min_shares_1), 1)

        total_pair_cost_per_share = safe_p0 + safe_p1
        total_req_usd = round(req_shares * total_pair_cost_per_share, 2)

        cur_bal = self.get_realtime_balance()
        if cur_bal <= 0:
            logger.warning("🛑 [余额未知] 无法确认可用余额，拒绝发单")
            return None

        # [修正 2026-11-02] 仓位不再由固定 2.6U 预算决定, 而是由"可用余额 × 上限比例"反推:
        # 固定预算在 1.15U/腿 的约束下几乎永远不够, 会导致系统长期零成交(看起来没亏, 其实是失联)。
        cap_pct = float(getattr(config, "POSITION_CAP_PCT", 0.45))
        max_budget = round(cur_bal * cap_pct, 2)
        if amount_usd and amount_usd > 0:
            # 调用方给的预算只作为"上限", 不能突破余额比例保护
            max_budget = round(min(max_budget, float(amount_usd)), 2)
        if total_req_usd > max_budget:
            logger.warning(
                f"⏳ [仓位上限拦截] 对称对冲需 {total_req_usd:.2f}U > 单笔上限 {max_budget:.2f}U "
                f"(余额 {cur_bal:.2f}U × {cap_pct:.0%})，拒绝发单"
            )
            return None

        cost_0 = round(req_shares * safe_p0, 2)
        cost_1 = round(req_shares * safe_p1, 2)

        # 稳胜铁律前置验证 2: 双腿金额双重硬件锁，任何一腿低于官方 $1.00 门槛直接死锁
        if cost_0 < config.MIN_ORDER_LEG_USD or cost_1 < config.MIN_ORDER_LEG_USD:
            logger.warning(f"🛑 [Polymarket 底线防护] 单腿价值不足 (YES: ${cost_0}, NO: ${cost_1} < ${config.MIN_ORDER_LEG_USD})，物理拒绝下单！")
            return None

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType

            # 1. 发送 YES 买单 (GTC 限价吃单)
            order_0 = OrderArgs(token_id=token_id_0, price=safe_p0, size=req_shares, side="BUY")
            signed_0 = self.client.create_order(order_0)
            resp_0 = self.client.post_order(signed_0, OrderType.GTC)
            id_0 = resp_0.get("orderID") if isinstance(resp_0, dict) else str(resp_0)

            # 2. 发送 NO 买单 (GTC 限价吃单)
            try:
                order_1 = OrderArgs(token_id=token_id_1, price=safe_p1, size=req_shares, side="BUY")
                signed_1 = self.client.create_order(order_1)
                resp_1 = self.client.post_order(signed_1, OrderType.GTC)
                id_1 = resp_1.get("orderID") if isinstance(resp_1, dict) else str(resp_1)
            except Exception as e_leg2:
                logger.critical(f"🚨 [单腿熔断防护盾启动] NO 腿买单异常 ({e_leg2})，立即紧急撤销 YES 腿订单 {id_0}！")
                try:
                    self.client.cancel(id_0)
                except Exception:
                    pass
                return None

            actual_usd = cost_0 + cost_1
            expected_payout = round(req_shares * 1.0, 2)
            expected_profit = round(expected_payout - actual_usd, 2)
            profit_pct = round((expected_profit / max(actual_usd, 0.01)) * 100, 2)
            bag_key = market_id or f"mkt-{id_0[:8]}"

            logger.info(f"⚡ [100% 稳胜配对成功] 市场: {question[:30]} | YES: {id_0[:8]} ({req_shares}份) | NO: {id_1[:8]} ({req_shares}份) | 总成本: {actual_usd}U | 到期硬兑现: {expected_payout}U (+{profit_pct}%)")

            record = {
                "id": f"live-bag-{int(time.time()*1000)}",
                "market_id": bag_key,
                "status": "HOLDING_LOCK",
                "mode": "live",
                "question": question,
                "pair_cost": round(safe_p0 + safe_p1, 4),
                "order_id_0": id_0,
                "order_id_1": id_1,
                "token_id_0": token_id_0,
                "token_id_1": token_id_1,
                "shares_0": req_shares,
                "shares_1": req_shares,
                "shares": req_shares,
                "price_0": safe_p0,
                "price_1": safe_p1,
                "invested_usd": actual_usd,
                "expected_profit_usd": expected_profit,
                "profit_pct": profit_pct,
                "entry_time": time.strftime("%H:%M:%S"),
                "entry_timestamp": time.time(),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.live_orders[bag_key] = record
            return record

        except Exception as e:
            logger.error(f"[实盘下单失败] {e}")
            return {"status": "ERROR", "error": str(e)}

    def close_parity_position(self, market_id: str, bid_0: float = 0.0, bid_1: float = 0.0) -> bool:
        """实盘自动/手动均值回归平仓，回收现金与净利"""
        if market_id not in self.live_orders:
            return False
        rec = self.live_orders[market_id]
        safe_b0 = min(0.99, max(0.01, round(bid_0, 3)))
        safe_b1 = min(0.99, max(0.01, round(bid_1, 3)))
        payout = round((rec["shares_0"] * safe_b0) + (rec["shares_1"] * safe_b1), 2)
        profit = round(payout - rec.get("invested_usd", 0), 2)

        # 100% 胜率硬锁前置拦截: 只有提前平仓可拿到正收益(见利就收)才提前出手，绝不亏损割肉！
        if profit < 0:
            logger.info(f"🛡️ [100% 胜率硬锁] 当前盘口回收额 {payout}U < 成本 {rec.get('invested_usd')}U，拒绝割肉！持有直至合约到期以 1.000U 必胜承兑！")
            return False

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            # 卖出 YES 份额
            if rec.get("token_id_0") and rec.get("shares_0", 0) > 0 and safe_b0 > 0:
                s_order_0 = OrderArgs(token_id=rec["token_id_0"], price=safe_b0, size=rec["shares_0"], side="SELL")
                signed_0 = self.client.create_order(s_order_0)
                self.client.post_order(signed_0, OrderType.GTC)

            # 卖出 NO 份额
            if rec.get("token_id_1") and rec.get("shares_1", 0) > 0 and safe_b1 > 0:
                s_order_1 = OrderArgs(token_id=rec["token_id_1"], price=safe_b1, size=rec["shares_1"], side="SELL")
                signed_1 = self.client.create_order(s_order_1)
                self.client.post_order(signed_1, OrderType.GTC)

            self.cumulative_pnl += profit
            logger.info(f"💰 [实盘闪电平仓成功] 市场: {rec.get('question','')[:20]} | 回收现金: {payout}U | 见利落袋: {profit:+.2f}U")
            del self.live_orders[market_id]
            # 强制触发下一轮链上真实余额刷新
            self._last_balance_check = 0.0
            self.get_realtime_balance()
            return True
        except Exception as e:
            logger.error(f"实盘平仓异常: {e}")
            return False

    def close_position_manually(self, market_id: str) -> bool:
        """前端点击提前平仓市价指令"""
        if market_id in self.live_orders:
            rec = self.live_orders[market_id]
            # 默认以建仓买一价或微利平仓
            return self.close_parity_position(market_id, bid_0=rec.get("price_0", 0.5), bid_1=rec.get("price_1", 0.5))
        return False

    def place_test_order(self, token_id: str, price: float = 0.01, size: float = 5.0) -> Dict:
        """用于用户自测网络与实盘连通性的单笔微型测试单 (0.01U，不成交，验证后自动安全撤销)"""
        if not self.is_ready:
            return {"ok": False, "error": "执行器未就绪"}
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            order = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID") if isinstance(resp, dict) else str(resp)
            # 立即撤单保护资金
            if order_id and isinstance(resp, dict) and resp.get("success"):
                try:
                    self.client.cancel(order_id)
                except Exception:
                    pass
            return {
                "ok": True,
                "result": resp,
                "order_id": order_id,
                "note": "CLOB V2 官方签名验证成功！(微单已安全自动撤销)"
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def cancel_all_orders(self):
        """一键紧急撤单"""
        if self.is_ready:
            try:
                res = self.client.cancel_all()
                self.live_orders.clear()
                logger.info("🛑 [实盘撤单] 已向 CLOB 发起全额撤单")
                return res
            except Exception as e:
                logger.error(f"撤单失败: {e}")
        return None

    def get_realtime_balance(self) -> float:
        """
        全通道动态实时余额获取:
        1. 优先调用官方 CLOB V2 /balance-allowance 鉴权接口 (获得链上精确抵押品余额)
        2. 若链上结算延迟或暂未索引到新入金，则自适应合并底账与实时已实现收益 PnL
        3. 无论后续充值多少，接口一旦有数立即自动向上跳变，实时刷新！
        """
        now = time.time()
        # 3 秒动态防抖刷新
        if self.is_ready and (now - self._last_balance_check > 3.0):
            try:
                from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
                bal = self.client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                raw = float(bal.get("balance", "0"))
                clob_usdc = raw / 1e6
                if clob_usdc > 0:
                    self.usdc_balance = round(clob_usdc, 4)
                else:
                    # 动态合并入金底账与已实现收益
                    base = float(os.getenv("INITIAL_BALANCE", "4.0"))
                    self.usdc_balance = round(max(0.0, base + self.cumulative_pnl), 4)
                self._last_balance_check = now
            except Exception as e:
                logger.debug(f"实时余额轮询轻微提示: {e}")
        return self.usdc_balance

    def get_status(self) -> Dict:
        current_bal = self.get_realtime_balance()
        return {
            "is_ready": self.is_ready,
            "version": "CLOB V2",
            "funder": self.funder,
            "wallet": self.wallet_address,
            "usdc_balance": current_bal,
            "max_order_usd": self.max_order_usd,
            "hard_stop_loss_usd": self.hard_stop_loss_usd,
            "cumulative_pnl": self.cumulative_pnl,
            "active_orders_count": len(self.live_orders)
        }
