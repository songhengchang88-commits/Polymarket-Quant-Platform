"""盘口快照采集器。

目的: 为 markout 分析积累真实时间序列。每轮抓取目标市场双腿的最优买卖价,
追加写入 JSONL。不做任何交易, 不产生资金变动。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from pmq import marketdata


@dataclass
class Snapshot:
    ts: float
    market_id: str
    question: str
    bid_0: float
    ask_0: float
    bid_1: float
    ask_1: float

    @property
    def bid_sum(self) -> float:
        return self.bid_0 + self.bid_1

    @property
    def mid_sum(self) -> float:
        return (self.bid_0 + self.ask_0 + self.bid_1 + self.ask_1) / 2.0


class Collector:
    def __init__(self, markets: List[Dict[str, Any]], path: str):
        self.markets = [m for m in markets if marketdata.extract_tokens(m)]
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def sample(self) -> int:
        tokens: List[str] = []
        pairs = []
        for market in self.markets:
            pair = marketdata.extract_tokens(market)
            if not pair:
                continue
            pairs.append((market, pair))
            tokens.extend(pair)

        books = marketdata.fetch_books_bulk(tokens)
        now = time.time()
        written = 0
        lines = []
        for market, pair in pairs:
            b0, b1 = books.get(pair[0]), books.get(pair[1])
            if b0 is None or b1 is None:
                continue
            snap = Snapshot(
                ts=now,
                market_id=str(market.get("id")),
                question=str(market.get("question", ""))[:80],
                bid_0=b0.bid, ask_0=b0.ask, bid_1=b1.bid, ask_1=b1.ask,
            )
            lines.append(json.dumps(asdict(snap), ensure_ascii=False))
            written += 1
        if lines:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return written


def load_snapshots(path: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.setdefault(str(row.get("market_id")), []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: float(r.get("ts", 0)))
    return out
