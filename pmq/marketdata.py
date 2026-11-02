"""行情适配层: 把 Polymarket REST 原始结构转成内核可用的 Book。

只做三件事: 取市场、取盘口、算最优买卖价并打时间戳。
不做任何"平滑/补价/假设"，宁可返回 None 让上层拒单。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from pmq.risk import Book

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
TIMEOUT = 8


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_orderbook(raw: Dict[str, Any], ts: Optional[float] = None, tick: float = 0.01) -> Optional[Book]:
    """CLOB /book 返回: {"bids":[{"price","size"}], "asks":[...]} (无序)。"""
    if not isinstance(raw, dict):
        return None
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    if not bids or not asks:
        return None
    try:
        best_bid = max(_to_float(b.get("price")) for b in bids if _to_float(b.get("price")) > 0)
        best_ask = min(_to_float(a.get("price")) for a in asks if _to_float(a.get("price")) > 0)
        bid_size = sum(_to_float(b.get("size")) for b in bids
                       if _to_float(b.get("price")) == best_bid)
        ask_size = sum(_to_float(a.get("size")) for a in asks
                       if _to_float(a.get("price")) == best_ask)
    except (TypeError, ValueError):
        return None
    if not (0 < best_bid < 1 and 0 < best_ask < 1):
        return None
    return Book(bid=best_bid, ask=best_ask, ts=ts if ts is not None else time.time(),
                tick=tick, bid_size=bid_size, ask_size=ask_size)


def fetch_markets(limit: int = 50, category: Optional[str] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit, "active": "true", "closed": "false",
                              "order": "volume24hr", "ascending": "false"}
    if category and category != "all":
        params["tag_slug"] = category
    resp = requests.get(f"{GAMMA_URL}/markets", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_markets_paged(limit: int = 300, category: Optional[str] = None,
                        page: int = 100) -> List[Dict[str, Any]]:
    """分页拉取; Gamma 单页上限较小, 一次性 limit=500 会静默截断。"""
    out: List[Dict[str, Any]] = []
    offset = 0
    while len(out) < limit:
        batch = max(0, min(page, limit - len(out)))
        if batch == 0:
            break
        try:
            rows = fetch_markets(limit=batch, category=category) if offset == 0 else _fetch_page(batch, offset, category)
        except Exception:
            break
        if not rows:
            break
        out.extend(rows)
        if len(rows) < batch:
            break
        offset += batch
    return out[:limit]


def _fetch_page(limit: int, offset: int, category: Optional[str]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit, "offset": offset, "active": "true",
                              "closed": "false", "order": "volume24hr", "ascending": "false"}
    if category and category != "all":
        params["tag_slug"] = category
    resp = requests.get(f"{GAMMA_URL}/markets", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def extract_tokens(market: Dict[str, Any]) -> Optional[tuple]:
    import json as _json

    tokens = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if isinstance(tokens, str):
        try:
            tokens = _json.loads(tokens)
        except _json.JSONDecodeError:
            return None
    if not isinstance(tokens, list) or len(tokens) < 2:
        return None
    return str(tokens[0]), str(tokens[1])


def fetch_book(token_id: str, tick: float = 0.01) -> Optional[Book]:
    resp = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    return parse_orderbook(resp.json(), ts=time.time(), tick=tick)


def fetch_pair_books(token_0: str, token_1: str, tick: float = 0.01) -> tuple:
    return fetch_book(token_0, tick), fetch_book(token_1, tick)


def fetch_books_bulk(token_ids: List[str], tick: float = 0.01, chunk: int = 100) -> Dict[str, Book]:
    """批量取盘口: POST /books, 一次最多 100 个 token。

    顺序 GET 单个盘口在 8 个市场时要 30s, 批量后同样规模约 1-2s,
    否则"高频扫描"只是口号。
    """
    out: Dict[str, Book] = {}
    for i in range(0, len(token_ids), chunk):
        batch = [str(t) for t in token_ids[i:i + chunk]]
        try:
            resp = requests.post(
                f"{CLOB_URL}/books",
                json=[{"token_id": t} for t in batch],
                timeout=TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            ts = time.time()  # 时间戳必须在响应返回之后打, 否则批量扫描会把自己判成"陈旧"
            payload = resp.json()
            rows = payload if isinstance(payload, list) else payload.get("books", [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = row.get("asset_id") or row.get("token_id")
                book = parse_orderbook(row, ts=ts, tick=tick)
                if token and book is not None:
                    out[str(token)] = book
        except Exception:
            continue
    return out

