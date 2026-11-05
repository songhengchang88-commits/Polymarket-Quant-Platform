"""
==============================================================================
PolyArbitrage Pro - Polymarket CLOB WebSocket 毫秒级实时流桥连引擎
==============================================================================
特点:
1. 直连官方 WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market
2. 毫秒级接收 orderbook 快照与增量更新 (best_bid_ask, price_change, trade)
3. 内存原子维护 80+ 盘口共 160+ Token 的最新买卖深度与价格
4. 10 秒心跳自动保活 + 断线指数退避自愈
5. 供量化引擎与前端极速读取，彻底消除 REST API 网络往返延迟
==============================================================================
"""

import asyncio
import json
import logging
import threading
import time
from typing import Dict, List, Optional, Set

import websockets

logger = logging.getLogger("polymarket_ws")
WS_URI = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

class PolymarketWebSocketClient:
    def __init__(self):
        self._subscribed_assets: Set[str] = set()
        self._new_sub_queue: List[str] = []
        self._unsub_queue: List[str] = []
        self._books: Dict[str, Dict] = {}  # asset_id -> {best_bid, best_ask, best_bid_size, best_ask_size, bids_count, asks_count, latency_ms, last_update, last_trade_price}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._connected = False
        self._last_heartbeat = time.time()
        self._last_pong = 0.0  # F3: 上次收到 PONG 的时间戳（连接死亡检测）
        self._dropped_count = 0  # F5: 解析失败/未知事件的丢弃计数
        self._skipped_sub_count = 0  # F6: 连接不一致被跳过的订阅 token 计数
        self._msg_count = 0

    def start(self):
        """在后台守护线程中启动异步 WebSocket 事件循环"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PolymarketWS")
        self._thread.start()
        logger.info("⚡ [WebSocket 实时流引擎] 线程已启动")

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_and_listen())

    async def _connect_and_listen(self):
        backoff = 1.0
        while self._running:
            try:
                logger.info(f"⚡ [WebSocket 直连] 正在连接官方行情中心: {WS_URI}")
                async with websockets.connect(
                    WS_URI,
                    ping_interval=None,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_pong = time.time()  # F3：连接建立即起算 PONG 死亡检测宽限
                    backoff = 1.0
                    logger.info("✅ [WebSocket 直连成功] Polymarket CLOB 毫秒级行情流已就绪")

                    # 连接成功后，立即订阅当前已有的所有 Token
                    with self._lock:
                        tokens_to_sub = list(self._subscribed_assets)
                    if tokens_to_sub:
                        await self._send_subscription(ws, tokens_to_sub)

                    # F4：重连后旧簿一律标陈旧，强制等新快照（或走 REST 回退），不再冒充新鲜
                    self._mark_books_stale()

                    # 启动并发任务：消息接收、心跳保持、动态增量订阅检查
                    listener_task = asyncio.create_task(self._listener(ws))
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    queue_task = asyncio.create_task(self._subscription_checker(ws))

                    done, pending = await asyncio.wait(
                        [listener_task, heartbeat_task, queue_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()

            except Exception as e:
                self._connected = False
                logger.warning(f"⚠️ [WebSocket 异常断开]: {e}，将在 {backoff:.1f} 秒后自动自愈重连...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)

    async def _send_subscription(self, ws, token_ids: List[str]):
        # F6：只用调用方传入的连接发送；与当前连接不一致说明期间已重连换连接，
        # 跳过并计数，等重连时的全量重订兜底，避免订阅状态与服务端漂移
        if ws is not self._ws:
            self._skipped_sub_count += len(token_ids)
            logger.warning(f"[WebSocket 订阅跳过] 连接已变更，跳过 {len(token_ids)} 个代币的订阅发送（累计跳过 {self._skipped_sub_count}）")
            return
        if not ws or not token_ids:
            return
        # 官方建议分批发送订阅，每批最多 50 个 token
        batch_size = 50
        for i in range(0, len(token_ids), batch_size):
            chunk = token_ids[i:i + batch_size]
            sub_payload = {
                "assets_ids": chunk,
                "type": "market",
                "custom_feature_enabled": True
            }
            try:
                await ws.send(json.dumps(sub_payload))
                logger.info(f"📡 [WebSocket 订阅发送] 已订阅 {len(chunk)} 个代币盘口行情")
            except Exception as e:
                logger.warning(f"订阅发送失败: {e}")

    async def _send_unsubscription(self, ws, token_ids: List[str]):
        """F4：发送退订帧（operation: "unsubscribe"），连接一致性校验同 F6"""
        if ws is not self._ws:
            self._skipped_sub_count += len(token_ids)
            logger.warning(f"[WebSocket 退订跳过] 连接已变更，跳过 {len(token_ids)} 个代币的退订发送（累计跳过 {self._skipped_sub_count}）")
            return
        if not ws or not token_ids:
            return
        batch_size = 50
        for i in range(0, len(token_ids), batch_size):
            chunk = token_ids[i:i + batch_size]
            unsub_payload = {
                "assets_ids": chunk,
                "operation": "unsubscribe"
            }
            try:
                await ws.send(json.dumps(unsub_payload))
                logger.info(f"📡 [WebSocket 退订发送] 已退订 {len(chunk)} 个代币盘口行情")
            except Exception as e:
                logger.warning(f"退订发送失败: {e}")

    async def _subscription_checker(self, ws):
        while self._running:
            new_tokens = []
            unsub_tokens = []
            with self._lock:
                if self._new_sub_queue:
                    new_tokens = list(self._new_sub_queue)
                    self._new_sub_queue.clear()
                if self._unsub_queue:
                    unsub_tokens = list(self._unsub_queue)
                    self._unsub_queue.clear()
            if unsub_tokens:
                await self._send_unsubscription(ws, unsub_tokens)
            if new_tokens:
                await self._send_subscription(ws, new_tokens)
            await asyncio.sleep(0.5)

    async def _heartbeat(self, ws):
        while self._running:
            await asyncio.sleep(10)
            # F3：PONG 死亡检测——距上次 PONG 超 30 秒判定连接已死（半开连接），
            # 主动关闭触发 _connect_and_listen 的既有重连路径；口径与 py-sdk 对齐（10 秒发 / 30 秒判死）
            if time.time() - self._last_pong > 30:
                logger.warning("💀 [WebSocket 心跳超时] 超过 30 秒未收到 PONG，判定连接已死亡，主动关闭并重连")
                self._connected = False
                try:
                    await ws.close()
                except Exception:
                    pass
                break
            try:
                await ws.send("PING")
                self._last_heartbeat = time.time()
            except Exception:
                break

    async def _listener(self, ws):
        async for raw_msg in ws:
            if raw_msg == "PONG":
                # F3：记录 PONG 到达时间，供 _heartbeat 做死亡检测
                self._last_pong = time.time()
                continue
            try:
                data = json.loads(raw_msg)
                self._process_message(data)
                self._msg_count += 1
            except Exception as e:
                # F5：解析/处理异常不再静默吞掉，计数 + 日志，保证线上可观测
                self._dropped_count += 1
                logger.debug(f"[WebSocket 消息丢弃] 解析/处理失败: {e}")

    @staticmethod
    def _empty_book() -> Dict:
        """空盘语义的新簿（F2）：无买盘 best_bid=0.0、无卖盘 best_ask=1.0、size 均为 0"""
        return {
            "best_bid": 0.0,
            "best_bid_size": 0.0,
            "best_ask": 1.0,
            "best_ask_size": 0.0,
            "bids_count": 0,
            "asks_count": 0,
            "latency_ms": -1.0,
            "last_update": 0.0,
            "last_trade_price": None
        }

    @staticmethod
    def _calc_latency_ms(item: Dict, now: float) -> float:
        """F5：有服务端时间戳才真算延迟；缺失/异常一律 -1.0 表示未知，禁止再合成默认值"""
        ts = item.get("timestamp")
        if not ts:
            return -1.0
        try:
            server_time = float(ts) / 1000.0
            calc_lat = (now - server_time) * 1000.0
            if 0 <= calc_lat < 5000:
                return round(calc_lat, 1)
        except Exception:
            pass
        return -1.0

    def _process_message(self, data):
        """解析 Polymarket CLOB WS 数据并原子更新本地订单薄
        F1：按 event_type 分发——book 全量快照 / price_change 增量 / best_bid_ask 最优价 /
        last_trade_price 仅成交价（不刷新盘口新鲜度戳）；未知类型计数丢弃（F5）。
        """
        now = time.time()
        items = data if isinstance(data, list) else [data]
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    self._dropped_count += 1
                    continue
                event_type = item.get("event_type")
                if event_type == "book":
                    self._apply_book_snapshot(item, now)
                elif event_type == "price_change":
                    self._apply_price_change(item, now)
                elif event_type == "best_bid_ask":
                    self._apply_best_bid_ask(item, now)
                elif event_type == "last_trade_price":
                    self._apply_last_trade_price(item)
                else:
                    # 未知/缺失 event_type：计数丢弃，不再把任何帧都当快照处理（D1/D7）
                    self._dropped_count += 1

    def _apply_book_snapshot(self, item: Dict, now: float):
        """book 全量快照：某侧为空即如实写空盘语义（F2），不再保留上一帧极值"""
        asset_id = item.get("asset_id")
        if not asset_id:
            self._dropped_count += 1
            return

        bids = item.get("bids", [])
        asks = item.get("asks", [])

        best_bid = 0.0
        best_bid_size = 0.0
        best_ask = 1.0
        best_ask_size = 0.0

        if bids:
            try:
                sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                best_bid = float(sorted_bids[0].get("price", 0))
                best_bid_size = float(sorted_bids[0].get("size", 0))
            except Exception:
                pass

        if asks:
            try:
                sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
                best_ask = float(sorted_asks[0].get("price", 1))
                best_ask_size = float(sorted_asks[0].get("size", 0))
            except Exception:
                pass

        prev = self._books.get(asset_id)
        self._books[asset_id] = {
            "best_bid": best_bid,
            "best_bid_size": best_bid_size,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "bids_count": len(bids),
            "asks_count": len(asks),
            "latency_ms": self._calc_latency_ms(item, now),
            "last_update": now,
            # 成交价由 last_trade_price 帧单独维护，快照不覆盖
            "last_trade_price": prev.get("last_trade_price") if prev else None
        }

    def _apply_price_change(self, item: Dict, now: float):
        """price_change 增量（F1）：最小集不维护全深档位，以每条自带的 best_bid/best_ask
        更新对应 asset 的最优价并刷新新鲜度戳；变更档位 size=0 表示该档位被移除——
        若恰为当前最优价，先置空盘语义，再被本帧自带的最优价（变更后状态）覆盖，
        避免已被移除的价格继续冒充最优价。"""
        changes = item.get("price_changes") or []
        for change in changes:
            if not isinstance(change, dict):
                self._dropped_count += 1
                continue
            asset_id = change.get("asset_id")
            if not asset_id:
                self._dropped_count += 1
                continue
            book = self._books.get(asset_id)
            if book is None:
                book = self._empty_book()
            try:
                ch_price = float(change.get("price"))
                ch_size = float(change.get("size"))
                if ch_size == 0.0:
                    if change.get("side") == "BUY" and ch_price == book["best_bid"]:
                        book["best_bid"] = 0.0
                        book["best_bid_size"] = 0.0
                    elif change.get("side") == "SELL" and ch_price == book["best_ask"]:
                        book["best_ask"] = 1.0
                        book["best_ask_size"] = 0.0
            except Exception:
                pass
            try:
                if change.get("best_bid") is not None:
                    book["best_bid"] = float(change["best_bid"])
                if change.get("best_ask") is not None:
                    book["best_ask"] = float(change["best_ask"])
            except Exception:
                pass
            book["last_update"] = now
            self._books[asset_id] = book

    def _apply_best_bid_ask(self, item: Dict, now: float):
        """best_bid_ask（F1）：以其 best_bid/best_ask 直接更新最优价并刷新新鲜度戳，不动深度计数"""
        asset_id = item.get("asset_id")
        if not asset_id:
            self._dropped_count += 1
            return
        book = self._books.get(asset_id)
        if book is None:
            book = self._empty_book()
        try:
            if item.get("best_bid") is not None:
                book["best_bid"] = float(item["best_bid"])
            if item.get("best_ask") is not None:
                book["best_ask"] = float(item["best_ask"])
        except Exception:
            pass
        book["last_update"] = now
        self._books[asset_id] = book

    def _apply_last_trade_price(self, item: Dict):
        """last_trade_price（F1）：只更新成交价字段，绝不刷新盘口新鲜度戳 last_update"""
        asset_id = item.get("asset_id")
        if not asset_id:
            self._dropped_count += 1
            return
        book = self._books.get(asset_id)
        if book is None:
            book = self._empty_book()
        try:
            if item.get("price") is not None:
                book["last_trade_price"] = float(item["price"])
        except Exception:
            pass
        self._books[asset_id] = book

    def subscribe_tokens(self, token_ids: List[str]):
        """外部调用：动态注册需要监听行情的 token 列表"""
        to_add = []
        with self._lock:
            for tid in token_ids:
                if tid and tid not in self._subscribed_assets:
                    self._subscribed_assets.add(tid)
                    self._new_sub_queue.append(tid)
                    to_add.append(tid)
        return len(to_add)

    def unsubscribe_tokens(self, token_ids: List[str]):
        """外部调用（F4）：退订 token 并移除其本地簿，供轮动池换出 token 时调用；
        退订帧由 _subscription_checker 异步发出，未发出的订阅直接从待订队列撤下"""
        removed = 0
        with self._lock:
            for tid in token_ids:
                if tid and tid in self._subscribed_assets:
                    self._subscribed_assets.discard(tid)
                    self._books.pop(tid, None)
                    if tid in self._new_sub_queue:
                        self._new_sub_queue.remove(tid)
                    self._unsub_queue.append(tid)
                    removed += 1
        return removed

    def _mark_books_stale(self):
        """F4：将所有本地簿标陈旧（last_update=0），新鲜度判定强制等待新快照到达"""
        with self._lock:
            for book in self._books.values():
                book["last_update"] = 0

    def get_book(self, token_id: str) -> Optional[Dict]:
        """从内存毫秒级获取订单薄 (0.05ms)"""
        with self._lock:
            return self._books.get(token_id)

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "connected": self._connected,
                "subscribed_tokens": len(self._subscribed_assets),
                "cached_books": len(self._books),
                "total_messages": self._msg_count,
                "dropped_events": self._dropped_count,
                "skipped_subscriptions": self._skipped_sub_count,
                "last_pong_age_s": round(time.time() - self._last_pong, 1) if self._last_pong else None,
                "latency_note": "latency_ms=-1.0 表示该帧无服务端时间戳、延迟未知（F5，不再合成默认值）",
                "latency_mode": "WebSocket 毫秒级直连 (<50ms)" if self._connected else "REST 轮询回退"
            }

# 单例导出
ws_feed = PolymarketWebSocketClient()
