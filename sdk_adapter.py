"""
sdk_adapter.py — py-sdk 迁移适配层（阶段 1 步 0 骨架 + 步 1 行情只读）

定位（依据 hidden_files/stage1/py-sdk迁移映射.md 迁移顺序 步 0 / 步 1）：
- 本模块是现行代码与官方 py-sdk（polymarket-client==0.16.0）之间的唯一隔离层。
  对上只暴露我方模型（与 polymarket.py / polymarket_ws.py 现行输出结构对齐的 dict），
  对下封装 py-sdk 的 PublicClient（只读）；SDK 的 pydantic 模型不外泄到调用方。
- 本步只实现行情只读：订单簿（单个/批量）、市场元数据、最新成交价/中间价/价差、tick 查询。
- 下单 / 撤单 / 账户等写路径只保留接口签名，一律抛 AdapterNotEnabledError，
  严禁以假实现充数；启用步骤见迁移映射 步 3 / 步 4。
- 心跳（订单死人开关）本步不实现，仅预留 HeartbeatPort 接口位置；
  实现规格见 hidden_files/stage1/心跳自研规格.md，整合在迁移 步 5。
- 本模块不 import 旧 py_clob_client_v2 / py_clob_client，可独立 import。

我方 Book 结构（与 polymarket.py:get_orderbook 的 REST 输出对齐，并做全深度扩展）：
    {
        "token_id": str,
        "best_bid": float,          # 无买盘时 0.0（沿用旧语义）
        "best_bid_size": float,
        "best_ask": float,          # 无卖盘时 1.0（沿用旧语义）
        "best_ask_size": float,
        "depth_count": int,         # bids 档数 + asks 档数
        "bids_count": int,
        "asks_count": int,
        "latency_ms": float,        # 本次拉取耗时（旧 REST 路径同语义）
        # —— 以下为扩展字段（旧结构没有，影子比对/步 1 起新增）——
        "bids": [{"price": float, "size": float}, ...],   # 按价格降序
        "asks": [{"price": float, "size": float}, ...],   # 按价格升序
        "tick_size": float | None,
        "min_order_size": float | None,
        "last_trade_price": float | None,
        "neg_risk": bool | None,
        "condition_id": str | None,
        "timestamp": float | None,  # 服务端簿时间戳（epoch 秒）
    }

我方 Market 结构（与 polymarket.py:get_curated_markets 的输出核心字段对齐）：
    {
        "id": str, "question": str, "slug": str,
        "clob_token_ids": [yes_token, no_token],
        "outcome_0": str, "outcome_1": str,
        "volume": float | None, "volume24hr": float | None, "liquidity": float | None,
        "end_date": str,                       # ISO 字符串
        "condition_id": str | None,
        "active": bool | None, "closed": bool | None,
        "best_bid": float | None, "best_ask": float | None,
        "last_trade_price": float | None,
        "tick_size": float | None,             # trading.minimum_tick_size
        "min_order_size": float | None,
    }
    注：旧输出中的 category / tag_badge / is_today 是 polymarket.py 内部的
    业务分类推导（关键词规则），不属于行情元数据，本适配层不提供，
    后续切换时由 polymarket.py 在适配层输出之上继续推导。
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Dict, List, Optional, Protocol, Sequence

__all__ = [
    "AdapterNotEnabledError",
    "HeartbeatPort",
    "NullHeartbeatPort",
    "SdkReadOnlyAdapter",
    "orderbook_to_dict",
    "market_to_dict",
]

# py-sdk 锁版本（0.x 的 minor 版本可能含 breaking change，见 SDK README）
SDK_VERSION_PIN = "0.16.0"


class AdapterNotEnabledError(RuntimeError):
    """写路径 / 未启用能力被调用时抛出。适配层严禁用假实现代替未启用能力。"""


# ---------------------------------------------------------------------------
# 心跳接口预留（本步不实现；规格见 心跳自研规格.md，整合于迁移 步 5）
# ---------------------------------------------------------------------------

class HeartbeatPort(Protocol):
    """订单心跳（死人开关）端口。

    语义锚点（规格 §2/§3）：SecureClient.send_order_heartbeat(heartbeat_id) 单次发送；
    首发 heartbeat_id=""，响应轮换下发新 ID 须串行回传；每 5 秒一发；
    断供 10 秒服务端尝试全撤（尽力而为）；无 disarm 操作。
    """

    def start(self) -> None:
        """启动 5 秒周期发送循环（武装死人开关）。"""
        ...

    def stop(self, *, cancel_first: bool = True) -> None:
        """停机善后：先撤单再停发（规格 §3.4），不得直接停发裸奔。"""
        ...

    def send_once(self) -> str:
        """发送一次并返回下一轮应携带的 heartbeat_id。"""
        ...


class NullHeartbeatPort:
    """心跳占位实现：所有操作抛 AdapterNotEnabledError（步 5 才接真实实现）。"""

    def start(self) -> None:
        raise AdapterNotEnabledError("订单心跳未启用：自研循环归迁移步 5（见心跳自研规格.md）")

    def stop(self, *, cancel_first: bool = True) -> None:
        raise AdapterNotEnabledError("订单心跳未启用：自研循环归迁移步 5（见心跳自研规格.md）")

    def send_once(self) -> str:
        raise AdapterNotEnabledError("订单心跳未启用：自研循环归迁移步 5（见心跳自研规格.md）")


# ---------------------------------------------------------------------------
# 转换函数（纯函数：SDK pydantic 模型 -> 我方 dict；可独立自测）
# ---------------------------------------------------------------------------

def _f(value) -> Optional[float]:
    """Decimal / str / int -> float；None 透传。"""
    if value is None:
        return None
    return float(value)


def orderbook_to_dict(book, latency_ms: float = 0.0) -> Dict:
    """py-sdk OrderBook -> 我方 Book dict。

    排序不信任上游约定（加固 A-06）：SDK 仅以 docstring 约定 bids 升序、
    asks 降序且无 validator，本函数一律按价格显式排序（bids 降序 /
    asks 升序），best_* 由排序后首档推导，全链单一口径。
    空侧 best_* 输出 None（加固 A-04）：旧 0.0/1.0 哨兵会被下游当真实
    价格算出 100%/70% 的伪造价差，None 才能让消费者走「无数据」口径。
    """
    bids_desc = sorted(
        ({"price": float(level.price), "size": float(level.size)}
         for level in book.bids),
        key=lambda lv: lv["price"], reverse=True)
    asks_asc = sorted(
        ({"price": float(level.price), "size": float(level.size)}
         for level in book.asks),
        key=lambda lv: lv["price"])
    ts = book.timestamp
    return {
        "token_id": str(book.asset_id),
        "best_bid": bids_desc[0]["price"] if bids_desc else None,
        "best_bid_size": bids_desc[0]["size"] if bids_desc else 0.0,
        "best_ask": asks_asc[0]["price"] if asks_asc else None,
        "best_ask_size": asks_asc[0]["size"] if asks_asc else 0.0,
        "depth_count": len(bids_desc) + len(asks_asc),
        "bids_count": len(bids_desc),
        "asks_count": len(asks_asc),
        "latency_ms": latency_ms,
        "bids": bids_desc,
        "asks": asks_asc,
        "tick_size": _f(book.tick_size),
        "min_order_size": _f(book.min_order_size),
        "last_trade_price": _f(book.last_trade_price),
        "neg_risk": bool(book.neg_risk) if book.neg_risk is not None else None,
        "condition_id": str(book.condition_id) if book.condition_id else None,
        "timestamp": ts.timestamp() if ts is not None else None,
    }


def market_to_dict(market) -> Dict:
    """py-sdk gamma Market -> 我方 Market dict（字段对照见模块 docstring）。"""
    outcomes = market.outcomes
    yes, no = outcomes.yes, outcomes.no
    metrics = market.metrics
    prices = market.prices
    trading = market.trading
    state = market.state
    end_date = state.end_date
    volume = _f(metrics.volume_num if metrics.volume_num is not None else metrics.volume)
    return {
        "id": str(market.id),
        "question": market.question or "",
        "slug": market.slug or "",
        "clob_token_ids": [
            str(yes.token_id) if yes.token_id else "",
            str(no.token_id) if no.token_id else "",
        ],
        "outcome_0": yes.label or "Yes",
        "outcome_1": no.label or "No",
        # 量字段保留 None（加固 A-05）：`or 0.0` 会把 Gamma「无数据」
        # 压成真实零成交，scoring 的 unknown 三态分支成死代码、否决
        # 留痕失真。缺失就报缺失，由 scoring 裁决。
        "volume": volume,
        "volume24hr": _f(metrics.volume_24hr),
        "liquidity": _f(metrics.liquidity_num if metrics.liquidity_num is not None else metrics.liquidity),
        "end_date": end_date.isoformat() if end_date is not None else "",
        "condition_id": str(market.condition_id) if market.condition_id else None,
        "active": state.active,
        "closed": state.closed,
        "best_bid": _f(prices.best_bid),
        "best_ask": _f(prices.best_ask),
        "last_trade_price": _f(prices.last_trade_price),
        "tick_size": _f(trading.minimum_tick_size),
        "min_order_size": _f(trading.minimum_order_size),
    }


# ---------------------------------------------------------------------------
# 环境细节（自拟，交付说明中注明）：本机 no_proxy 含 httpx 无法解析的
# [IPv6] 条目，py-sdk 构造 httpx 客户端时 trust_env 会因此抛 InvalidURL。
# 适配层在构造 SDK 客户端前，于本进程内清洗 no_proxy/NO_PROXY 中
# 带方括号的条目（仅影响本进程，不改系统环境）。
# ---------------------------------------------------------------------------

def _sanitize_no_proxy_env() -> None:
    for key in ("no_proxy", "NO_PROXY"):
        raw = os.environ.get(key)
        if raw and "[" in raw:
            cleaned = ",".join(p.strip() for p in raw.split(",") if "[" not in p)
            os.environ[key] = cleaned


# ---------------------------------------------------------------------------
# 适配层主体
# ---------------------------------------------------------------------------

class SdkReadOnlyAdapter:
    """py-sdk 只读适配层。构造时可注入现成 PublicClient（自测/影子比对用）。"""

    def __init__(self, public_client=None, heartbeat: Optional[HeartbeatPort] = None):
        self._public = public_client  # 延迟构造：import 本模块不触发网络/环境依赖
        self.heartbeat: HeartbeatPort = heartbeat if heartbeat is not None else NullHeartbeatPort()

    # -- 内部 ---------------------------------------------------------------

    def _client(self):
        if self._public is None:
            _sanitize_no_proxy_env()
            from polymarket import PublicClient  # py-sdk（polymarket-client==0.16.0）

            self._public = PublicClient()
        return self._public

    # -- 行情只读（步 1） -----------------------------------------------------

    def get_orderbook(self, token_id: str) -> Dict:
        """单个订单簿 -> 我方 Book dict（对应旧 polymarket.py:get_orderbook 的 REST 路径）。"""
        start = time.time()
        book = self._client().get_order_book(token_id=str(token_id))
        latency = (time.time() - start) * 1000
        return orderbook_to_dict(book, latency_ms=latency)

    def get_bulk_orderbooks(self, token_ids: Sequence[str]) -> Dict[str, Dict]:
        """批量订单簿（对应旧 get_bulk_orderbooks；SDK 单次批量请求替代 24 线程并发）。

        返回按 token_id 索引；以返回模型的 asset_id 对齐，不假设与入参同序。
        """
        token_ids = [str(t) for t in token_ids]
        if not token_ids:
            return {}
        start = time.time()
        books = self._client().get_order_books(token_ids=token_ids)
        latency = (time.time() - start) * 1000
        # SDK 未返回的 token 直接缺席，不再补空盘占位（加固 A-02）：占位盘
        # 在 build_pool 里触发否决、在 step_round 里却被当「在场」绕过
        # 失败判定，同一哨兵两个消费方语义相反。缺席是唯一诚实表示——
        # runner 的「腿缺席 → 数据失败」路径会接住它。
        return {str(b.asset_id): orderbook_to_dict(b, latency_ms=latency)
                for b in books}

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """最新成交价（无成交返回 None）。"""
        result = self._client().get_last_trade_price(token_id=str(token_id))
        if result is None:
            return None
        return _f(result.price)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        return _f(self._client().get_midpoint(token_id=str(token_id)))

    def get_spread(self, token_id: str) -> Optional[float]:
        return _f(self._client().get_spread(token_id=str(token_id)))

    def list_top_markets(self, limit: int = 100) -> List[Dict]:
        """按 24h 成交量降序的市场列表（未关闭）-> 我方 Market dict 列表。

        对应旧 polymarket.py:get_top_volume_markets 的数据来源（gamma /markets），
        但不含旧代码的本地筛选/分类/轮动逻辑（那是消费方职责）。
        """
        # Gamma 单页上限 100：旧实现只取 first_page()，limit>100 也只能拿到 100 个
        # 候选（2026-11-07 实测：limit=300 仍只回 100）。改走 iter_items() 跨页取满。
        paginator = self._client().list_markets(
            closed=False, order="volume24hr", ascending=False, page_size=100
        )
        items = []
        for m in paginator.iter_items():
            items.append(market_to_dict(m))
            if len(items) >= limit:
                break
        return items

    def get_market_by_token(self, token_id: str) -> Optional[Dict]:
        """按 clob token 反查市场元数据（SDK get_market 无 token 入口，走 list_markets 过滤）。"""
        paginator = self._client().list_markets(clob_token_ids=str(token_id), page_size=1)
        items = paginator.first_page().items
        if not items:
            return None
        return market_to_dict(items[0])

    def get_tick_size(self, token_id: str) -> Optional[float]:
        """tick 查询（替代旧 client.get_tick_size，映射 C5）：
        优先市场元数据 trading.minimum_tick_size，取不到时回退订单簿 tick_size。
        """
        market = self.get_market_by_token(token_id)
        if market and market.get("tick_size") is not None:
            return market["tick_size"]
        book = self.get_orderbook(token_id)
        return book.get("tick_size")

    # -- 写路径 / 账户 / 心跳：只留签名，一律未启用（步 3/4/5 才接） -----------

    def _not_enabled(self, what: str, step: str):
        raise AdapterNotEnabledError(
            f"{what} 未启用：适配层当前仅行情只读（步 0/1），{what} 归迁移{step}，"
            "启用前须完成该步验收，严禁用占位实现代替。"
        )

    def get_balance_allowance(self, *args, **kwargs):
        self._not_enabled("账户余额查询", "步 3（账户只读迁移）")

    def place_limit_order(self, *args, **kwargs):
        self._not_enabled("限价下单", "步 4（下单链路迁移）")

    def place_market_order(self, *args, **kwargs):
        self._not_enabled("市价下单", "步 4（下单链路迁移）")

    def cancel_order(self, *args, **kwargs):
        self._not_enabled("单笔撤单", "步 4（下单链路迁移）")

    def cancel_orders(self, *args, **kwargs):
        self._not_enabled("批量撤单", "步 4（下单链路迁移）")

    def cancel_all(self, *args, **kwargs):
        self._not_enabled("紧急全撤", "步 4（下单链路迁移）")

    def send_order_heartbeat(self, *args, **kwargs):
        self._not_enabled("订单心跳发送", "步 5（心跳整合，经 HeartbeatPort 注入实现）")
