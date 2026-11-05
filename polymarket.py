import json
import logging
import os
import time
from typing import Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import config
from polymarket_ws import ws_feed

logger = logging.getLogger("polymarket_client")
CACHE_EVENTS_FILE = os.path.join(config.DATA_DIR, "cached_events.json")

class PolymarketClient:
    """
    Polymarket CLOB & Gamma API 高速客户端 (全品类长尾穿透版 + WebSocket 实时毫秒级流引擎)
    """
    def __init__(self):
        self.session = requests.Session()
        retries = Retry(total=1, backoff_factor=0.05, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=100)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        
        # 启动官方 WebSocket 实时流引擎
        ws_feed.start()
        
        self._cached_events = []
        self._cache_time = 0.0
        self._pool_offset = 0
        self._last_rotation_time = time.time()
        self._rotation_wave = 1
        # 始终以官方最新预测市场数据启动，不依赖陈旧的磁盘事件文件
        self._cached_events = []
        self._cache_time = 0.0

    def get_live_public_trades(self, limit: int = 15) -> List[Dict]:
        """拉取全网最新真实成交流水"""
        url = "https://data-api.polymarket.com/trades"
        try:
            resp = self.session.get(url, params={"limit": limit}, timeout=3)
            if resp.status_code == 200:
                raw_trades = resp.json()
                formatted = []
                for t in raw_trades:
                    ts = t.get("timestamp", 0)
                    time_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
                    size = float(t.get("size") or 0)
                    price = float(t.get("price") or 0)
                    usd_val = round(size * price, 2)
                    title = t.get("title", "") or "Polymarket Event"
                    
                    formatted.append({
                        "id": t.get("transactionHash", "")[:10] or str(ts),
                        "timestamp": time_str,
                        "side": t.get("side", "BUY"),
                        "outcome": t.get("outcome", "Yes") or "Yes",
                        "title": title,
                        "price": price,
                        "size": size,
                        "usd_val": usd_val,
                        "trader": t.get("pseudonym") or t.get("name") or "Trader"
                    })
                return formatted
        except Exception as e:
            logger.warning(f"获取实时成交流水异常: {e}")
        return []

    def get_top_volume_markets(self, limit: int = config.SCAN_MARKET_LIMIT, category: str = "all") -> List[Dict]:
        return self.get_curated_markets(category=category, limit=limit)

    def get_curated_markets(self, category: str = "all", limit: int = config.SCAN_MARKET_LIMIT) -> List[Dict]:
        """
        Polymarket 官方预测市场 API (Predictions Overview) 实时数据流聚合引擎:
        直接对接 https://gamma-api.polymarket.com/markets 官方实时盘口流,
        汇聚 24h交易量巅峰榜 + 官方高流动性榜 + 加密货币专栏，
        彻底杜绝本地陈旧磁盘缓存、远期 2027/2028 僵尸盘与 0 成交衍生微单。
        """
        now = time.time()
        # 12 秒内存热更新周期: 既紧跟官网最新突发热点，又防止触发 API 限流
        if not self._cached_events or (now - self._cache_time) > 12.0:
            try:
                headers = {"User-Agent": config.USER_AGENT}
                aggregated_markets = []
                seen_market_ids = set()

                # 通道 1: 官方 24 小时真实成交量巅峰榜 Top 100 (与 polymarket.com 官网首页大盘严格同步)
                url_vol1 = f"{config.GAMMA_API_URL}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false"
                r1 = self.session.get(url_vol1, headers=headers, timeout=5)
                if r1.status_code == 200:
                    for m in r1.json():
                        mid = m.get("id")
                        if mid and mid not in seen_market_ids:
                            seen_market_ids.add(mid)
                            aggregated_markets.append(m)

                # 通道 2: 官方 24 小时成交量榜第 101-200 名 (扩充全平台深度池)
                url_vol2 = f"{config.GAMMA_API_URL}/markets?limit=100&offset=100&active=true&closed=false&order=volume24hr&ascending=false"
                r2 = self.session.get(url_vol2, headers=headers, timeout=5)
                if r2.status_code == 200:
                    for m in r2.json():
                        mid = m.get("id")
                        if mid and mid not in seen_market_ids:
                            seen_market_ids.add(mid)
                            aggregated_markets.append(m)

                # 通道 3: 官方实时最大流动性深度榜 Top 50
                url_liq = f"{config.GAMMA_API_URL}/markets?limit=50&active=true&closed=false&order=liquidity&ascending=false"
                r3 = self.session.get(url_liq, headers=headers, timeout=5)
                if r3.status_code == 200:
                    for m in r3.json():
                        mid = m.get("id")
                        if mid and mid not in seen_market_ids:
                            seen_market_ids.add(mid)
                            aggregated_markets.append(m)

                # 通道 4: 加密资产专属高频盘口 (BTC, ETH, SOL)
                url_crypto = f"{config.GAMMA_API_URL}/markets?limit=30&active=true&closed=false&tag_slug=crypto&order=volume24hr&ascending=false"
                r4 = self.session.get(url_crypto, headers=headers, timeout=4)
                if r4.status_code == 200:
                    for m in r4.json():
                        mid = m.get("id")
                        if mid and mid not in seen_market_ids:
                            seen_market_ids.add(mid)
                            aggregated_markets.append(m)

                if aggregated_markets:
                    self._cached_events = aggregated_markets
                    self._cache_time = now
            except Exception as e:
                logger.warning(f"官方 API 实时拉取轻微提示: {e}")

        crypto_keywords = ['bitcoin', 'btc', 'eth', 'sol', 'crypto', 'token', 'airdrop']
        sports_keywords = ['nba', 'champions', 'league', 'cup', 'premier', 'vs', 'cfc', 'fc', 'win', 'match', 'score', 'tennis']
        politics_keywords = ['election', 'president', 'trump', 'harris', 'governor', 'senate', 'fed', 'nomination', 'poll', 'israel', 'house', 'biden']

        import datetime
        today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        valid_markets = []

        for m in self._cached_events:
            if m.get("closed", False):
                continue

            q = m.get("question") or m.get("title", "")
            slug = m.get("slug", "")
            end_date = m.get("endDate", "") or ""
            full_text = f"{q} {slug} {end_date}".lower()
            vol24 = float(m.get("volume24hr") or 0)
            vol = float(m.get("volume") or 0)

            # 判断是否为当日发生 / 当日结算预测盘口
            is_today = False
            if today_str in end_date or tomorrow_str in end_date:
                is_today = True
            elif any(k in full_text for k in ['today', 'tonight', 'bo3', 'vs ', 'vs.', 'game 1', 'game 2', 'game 3', 'winner']):
                is_today = True

            # 分类筛选
            if category == "today" and not is_today:
                continue
            elif category == "crypto" and not any(k in full_text for k in crypto_keywords):
                continue
            elif category == "sports" and not any(k in full_text for k in sports_keywords):
                continue
            elif category == "politics" and not any(k in full_text for k in politics_keywords):
                continue

            # 严格过滤 1: 二元对冲代币解析必须有效 (Yes/No 两端)
            clob_tokens = m.get("clobTokenIds")
            if isinstance(clob_tokens, str):
                try:
                    clob_tokens = json.loads(clob_tokens)
                except Exception:
                    clob_tokens = []
            if not clob_tokens or len(clob_tokens) != 2:
                continue

            # 严格过滤 2: 远期沉睡死盘 (2027, 2028, 2029 等几年后才结算的死水盘口)
            if any(yr in q for yr in ['2027', '2028', '2029', '2030']):
                continue

            # 严格过滤 3: 无流动性死盘 (24h 成交额必须 >= 500U 或总成交额 >= 3000U，消灭 0/1.000 僵尸单)
            if vol24 < 500 and vol < 3000:
                continue

            # 严格过滤 4: 繁杂微型球员数据衍生盘 (只保留比赛胜负、大分或核心事件盘)
            if any(sub in q for sub in ['Assists O/U', 'Points O/U', 'Rebounds O/U', 'Steals O/U', 'Blocks O/U', 'Map 1 Winner', 'Map 2 Winner', 'Map 3', 'Round 1', 'Round 2']):
                continue

            outcomes = m.get("outcomes", '["Yes", "No"]')
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = ["Yes", "No"]

            tag_badge = "GENERAL"
            if is_today:
                tag_badge = "TODAY"
            elif any(k in full_text for k in sports_keywords):
                tag_badge = "SPORTS"
            elif any(k in full_text for k in politics_keywords):
                tag_badge = "POLITICS"
            elif any(k in full_text for k in ['bitcoin', 'btc']):
                tag_badge = "BTC"
            elif any(k in full_text for k in ['ethereum', 'eth']):
                tag_badge = "ETH"
            elif any(k in full_text for k in ['solana', 'sol']):
                tag_badge = "SOL"

            valid_markets.append({
                "id": m.get("id"),
                "question": q,
                "slug": slug,
                "clob_token_ids": clob_tokens,
                "outcome_0": outcomes[0] if len(outcomes) > 0 else "Yes",
                "outcome_1": outcomes[1] if len(outcomes) > 1 else "No",
                "volume": vol if vol > 0 else vol24,
                "volume24hr": vol24,
                "category": tag_badge.lower(),
                "tag_badge": tag_badge,
                "is_today": is_today,
                "end_date": end_date
            })

        # 当日事件给予加权置顶，其次按 24h 真实成交量降序
        valid_markets.sort(key=lambda x: (
            (20000000 if x["is_today"] else 0) + x.get("volume24hr", 0) * 1.5 + x["volume"]
        ), reverse=True)

        # 动态将所有有效盘口的代币 ID 注册到 WebSocket 极速行情监听池
        all_tokens = []
        for vm in valid_markets[:limit]:
            all_tokens.extend(vm.get("clob_token_ids", []))
        if all_tokens:
            ws_feed.subscribe_tokens(all_tokens)

        total_pool = len(valid_markets)
        if total_pool > limit:
            # 前 30 个锁定全网顶级热点主力大盘 (绝对置顶常驻扫描)，后 50 个高频平移轮动全网高成交长尾池
            anchor_count = min(30, total_pool)
            anchors = valid_markets[:anchor_count]
            rotating_pool = valid_markets[anchor_count:]

            rot_need = limit - anchor_count
            if rotating_pool:
                swapped_out_tokens = []
                if (now - self._last_rotation_time) > config.POOL_ROTATION_INTERVAL:
                    prev_rotating = rotating_pool[self._pool_offset : self._pool_offset + rot_need]
                    if len(prev_rotating) < rot_need:
                        prev_rotating += rotating_pool[:(rot_need - len(prev_rotating))]
                    swapped_out_tokens = [t for m in prev_rotating for t in m.get("clob_token_ids", [])]
                    self._pool_offset = (self._pool_offset + 12) % max(1, len(rotating_pool) - rot_need + 1)
                    self._last_rotation_time = now
                    self._rotation_wave += 1
                    logger.info(f"⚡ [全网实时轮动] 第 {self._rotation_wave} 波次 | 游标: {self._pool_offset}/{len(rotating_pool)}")

                selected_rotating = rotating_pool[self._pool_offset : self._pool_offset + rot_need]
                if len(selected_rotating) < rot_need:
                    selected_rotating += rotating_pool[:(rot_need - len(selected_rotating))]
                if swapped_out_tokens:
                    kept_tokens = {t for m in selected_rotating for t in m.get("clob_token_ids", [])}
                    ws_feed.unsubscribe_tokens([t for t in swapped_out_tokens if t not in kept_tokens])
                return anchors + selected_rotating

        return valid_markets[:limit]

    def get_orderbook(self, token_id: str) -> Dict:
        # 1. 优先尝试从 WebSocket 极速内存流读取 (0.05ms)
        ws_book = ws_feed.get_book(token_id)
        if ws_book and (time.time() - ws_book.get("last_update", 0)) < 4.0:
            return {
                "token_id": token_id,
                "best_bid": ws_book["best_bid"],
                "best_bid_size": ws_book["best_bid_size"],
                "best_ask": ws_book["best_ask"],
                "best_ask_size": ws_book["best_ask_size"],
                "depth_count": ws_book.get("bids_count", 0) + ws_book.get("asks_count", 0),
                "latency_ms": ws_book.get("latency_ms", 35.0)
            }

        # 2. REST 回退
        start = time.time()
        url = f"{config.CLOB_API_URL}/book"
        try:
            resp = self.session.get(url, params={"token_id": token_id}, timeout=1.2)
            latency = (time.time() - start) * 1000
            if resp.status_code == 200:
                data = resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                
                best_bid = 0.0
                best_bid_size = 0.0
                if bids:
                    parsed_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                    best_bid = float(parsed_bids[0].get("price", 0))
                    best_bid_size = float(parsed_bids[0].get("size", 0))

                best_ask = 1.0
                best_ask_size = 0.0
                if asks:
                    parsed_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
                    best_ask = float(parsed_asks[0].get("price", 1))
                    best_ask_size = float(parsed_asks[0].get("size", 0))

                return {
                    "token_id": token_id,
                    "best_bid": best_bid,
                    "best_bid_size": best_bid_size,
                    "best_ask": best_ask,
                    "best_ask_size": best_ask_size,
                    "depth_count": len(bids) + len(asks),
                    "latency_ms": latency
                }
        except Exception:
            pass
        return {"token_id": token_id, "best_bid": 0.0, "best_ask": 1.0, "latency_ms": 500}

    def get_bulk_orderbooks(self, token_ids: List[str]) -> Dict[str, Dict]:
        results = {}
        missed_tokens = []
        now = time.time()

        # 第一阶段: 从 WebSocket 毫秒级内存缓存提取已更新的代币 (0ms 零延迟)
        for tid in token_ids:
            ws_book = ws_feed.get_book(tid)
            if ws_book and (now - ws_book.get("last_update", 0)) < 4.0:
                results[tid] = {
                    "token_id": tid,
                    "best_bid": ws_book["best_bid"],
                    "best_bid_size": ws_book["best_bid_size"],
                    "best_ask": ws_book["best_ask"],
                    "best_ask_size": ws_book["best_ask_size"],
                    "depth_count": ws_book.get("bids_count", 0) + ws_book.get("asks_count", 0),
                    "latency_ms": ws_book.get("latency_ms", 35.0)
                }
            else:
                missed_tokens.append(tid)

        # 第二阶段: 仅对 WebSocket 未覆盖到的代币走多线程 REST 补充
        if missed_tokens:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=24) as executor:
                future_to_token = {executor.submit(self.get_orderbook, tid): tid for tid in missed_tokens}
                for future in future_to_token:
                    tid = future_to_token[future]
                    try:
                        results[tid] = future.result()
                    except Exception:
                        results[tid] = {"token_id": tid, "best_bid": 0.0, "best_ask": 1.0, "latency_ms": 500}

        return results
