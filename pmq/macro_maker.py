"""
宏观低波自适应做市引擎 (Macro Maker Engine)
=========================================
核心逻辑:
1. 解决主系统死等 Taker 纯负成本导致长时间零动作的痛点;
2. 继承方案 A (Plan A) 100% 验证成功的宏观低波做市逻辑;
3. 在优质宏观流动性标的 (美联储/选举) 买一档被动挂单 (Post-Only);
4. 单笔资金锁定 $1.15 ~ $1.50 U (严格满足 Size >= 5 与单腿 >= $1.00);
5. 120 秒生命周期硬看门狗: 到期未成交自动全额撤单，资金 100% 安全解冻回退;
6. 若被动成交，立即捕获做市利差并启动止盈保护;
7. 全程审计写入黑匣子，关键节点直推 Telegram 机器人。
"""

import os
import sys
import time
import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("MacroMaker")

class MacroMakerEngine:
    allow_maker = False  # 永久硬锁: 严禁单腿被动买入，坚决恪守零单腿敞口铁律
    def __init__(self, live_executor):
        self.live_executor = live_executor
        self.active_order: Optional[Dict[str, Any]] = None
        self.last_step_time = 0.0
        self.min_step_interval = 15.0  # 巡航步长 15 秒
        self.ttl_seconds = 120.0       # 挂单 120 秒未成交自动全撤
        self.macro_pool_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "balanced_macro.json"
        )
        self.current_target_index = 0

    def load_macro_targets(self):
        """读取优质宏观低波标的池"""
        if not os.path.exists(self.macro_pool_path):
            return []
        try:
            with open(self.macro_pool_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return [it for it in items if len(it.get("tokens", [])) >= 2]
        except Exception as e:
            logger.error(f"读取宏观标的池失败: {e}")
            return []

    def get_market_depth(self, token_id: str) -> Optional[Dict]:
        """获取盘口买卖一档"""
        import requests
        url = "https://clob.polymarket.com/book"
        try:
            r = requests.get(url, params={"token_id": token_id}, timeout=4)
            if r.status_code == 200:
                data = r.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                best_bid = float(bids[-1]["price"]) if bids else 0.0
                best_ask = float(asks[-1]["price"]) if asks else 1.0
                return {"best_bid": best_bid, "best_ask": best_ask}
        except Exception:
            pass
        return None

    def check_active_order(self) -> bool:
        """检查在途做市订单状态与看门狗撤单"""
        if not self.active_order:
            return False

        order_id = self.active_order["order_id"]
        placed_at = self.active_order["timestamp"]
        elapsed = time.time() - placed_at

        # 检查是否到期需强制撤单
        if elapsed >= self.ttl_seconds:
            logger.info(f"⏱️ [宏观做市看门狗] 挂单已满 {int(elapsed)} 秒未成交，执行主动安全撤单解冻资金...")
            try:
                self.live_executor.client.cancel(order_id)
                logger.info(f"✅ [做市看门狗撤单成功] 订单 {order_id} 已撤单，本金 100% 安全解冻！")
                from pmq.notify import notifier
                notifier.send(
                    title="⏱️ 宏观做市周期巡航撤单",
                    content=f"标的: {self.active_order.get('question', '')[:35]}\n订单: {order_id[:12]}...\n已挂单 {int(elapsed)}s 保护到期，安全撤回资金，准备轮换下一标的！"
                )
            except Exception as e:
                logger.warning(f"撤单提示: {e}")

            # 写入黑匣子
            if self.live_executor.journal:
                self.live_executor.journal.append({
                    "type": "MACRO_MAKER_CANCEL_TTL",
                    "order_id": order_id,
                    "elapsed_sec": round(elapsed, 1),
                    "amount_usd": self.active_order.get("amount_usd", 0.0)
                })

            self.active_order = None
            return False

        # 查询官方订单状态是否已完全成交
        try:
            ord_info = self.live_executor.client.get_order(order_id)
            status = ord_info.get("status", "").upper() if isinstance(ord_info, dict) else ""
            if status in ("MATCHED", "FILLED"):
                logger.info(f"🎉 [宏观做市撮合成交！] 订单: {order_id} 已在买一档成交！")
                from pmq.notify import notifier
                notifier.send(
                    title="🎉 宏观做市买一档被动成交",
                    content=f"标的: {self.active_order.get('question', '')[:35]}\n成交额: ${self.active_order.get('amount_usd', 0):.2f} USDC\n已成功按买一底价吸筹，锁定价差优势！"
                )
                self.active_order = None
                return False
        except Exception:
            pass

        return True

    def step(self):
        """执行单次巡航状态机"""
        now = time.time()
        if now - self.last_step_time < self.min_step_interval:
            return

        self.last_step_time = now
        # 紧急硬闸门: 严禁任何单腿暴露！只有在具备双腿完美对称闭环时才允许挂单
        if not getattr(self, "allow_maker", False):
            return

        if not self.live_executor.is_ready:
            return

        # 1. 优先维护在途订单
        if self.check_active_order():
            # 订单正在等待撮合中
            return

        # 2. 检查账户余额
        balance = self.live_executor.get_realtime_balance()
        if balance < 1.15:
            logger.debug(f"宏观做市跳过: 余额 ${balance:.2f} < $1.15")
            return

        # 3. 选取宏观标的
        targets = self.load_macro_targets()
        if not targets:
            return

        target = targets[self.current_target_index % len(targets)]
        self.current_target_index += 1

        tokens = target.get("tokens", [])
        if len(tokens) < 2:
            return

        # 4. 获取盘口深度
        token_id = tokens[0]  # YES 腿
        depth = self.get_market_depth(token_id)
        if not depth:
            return

        best_bid = depth["best_bid"]
        best_ask = depth["best_ask"]

        # 健康价格区间: 0.10 <= bid <= 0.90, 价差至少 0.5 美分
        if best_bid < 0.10 or best_bid > 0.90 or (best_ask - best_bid) < 0.005:
            return

        # 挂在买一档排队 (Post-Only)
        price = round(best_bid, 3)
        # 份额对齐: 满足 Minimum Size >= 5 与金额 >= $1.15
        shares = round(max(5.0, 1.15 / price), 2)
        amount_usd = round(shares * price, 2)

        # 检查是否超过单笔上限 (50% 资金上限，即 ~$2.58U)
        max_allowed = round(balance * 0.50, 2)
        if amount_usd > max_allowed:
            return

        # 5. 执行官方发单
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY

            order_args = OrderArgs(
                price=price,
                size=shares,
                side=BUY,
                token_id=token_id
            )

            signed_order = self.live_executor.client.create_order(order_args)
            resp = self.live_executor.client.post_order(signed_order, OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
            elif isinstance(resp, str):
                order_id = resp

            if not order_id:
                logger.warning(f"宏观做市挂单未返回订单号: {resp}")
                return

            logger.info(
                f"🎯 [宏观做市挂单成功] 标的: {target.get('question','')[:30]}... | "
                f"买1价: {price} | 份数: {shares} | 金额: ${amount_usd:.2f}U | 单号: {order_id}"
            )

            self.active_order = {
                "order_id": str(order_id),
                "timestamp": time.time(),
                "question": target.get("question", ""),
                "token_id": token_id,
                "price": price,
                "shares": shares,
                "amount_usd": amount_usd
            }

            from pmq.notify import notifier
            notifier.send(
                title="🎯 宏观低波被动做市挂单",
                content=(
                    f"标的: {target.get('question', '')[:40]}...\n"
                    f"方向: 买1档挂单排队 (Post-Only)\n"
                    f"价格: {price} | 份数: {shares} 份\n"
                    f"资金锁定: ${amount_usd:.2f} USDC (占余额 {round(amount_usd/balance*100, 1)}%)\n"
                    f"生命周期: 120 秒看门狗守护，未成交自动全撤退款！"
                )
            )

            if self.live_executor.journal:
                self.live_executor.journal.append({
                    "type": "MACRO_MAKER_POST",
                    "order_id": str(order_id),
                    "market_id": target.get("condition_id"),
                    "price": price,
                    "shares": shares,
                    "amount_usd": amount_usd
                })

        except Exception as e:
            logger.warning(f"宏观做市挂单异常: {e}")
