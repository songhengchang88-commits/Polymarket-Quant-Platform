"""多结果事件(negRisk)荷兰书扫描。

二元市场只需 ask0+ask1 < 1; 多结果事件则是"把所有结果的 YES 卖价加总",
总和 < 1.00 意味着可以一次性买下全部结果, 到期必然有一份兑付 1.00。
这是二元平价套利消失后, 理论上仍然可能存在的无风险结构。
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmq import marketdata


def main(limit: int = 600, min_outcomes: int = 3) -> int:
    import requests

    markets = marketdata.fetch_markets_paged(limit=limit)
    groups = defaultdict(list)
    for market in markets:
        events = market.get("events") or []
        if not events:
            continue
        gid = str(events[0].get("id"))
        groups[gid].append(market)

    candidates = {gid: rows for gid, rows in groups.items() if len(rows) >= min_outcomes}
    print(f"市场 {len(markets)} 个, 事件组 {len(groups)} 个, 结果数 >= {min_outcomes} 的组 {len(candidates)} 个")
    print("注意: 必须用完整事件的全部结果, 只取采样到的子集会把合计算小(假套利)")

    rows_out = []
    for gid in candidates:
        try:
            resp = requests.get(f"{marketdata.GAMMA_URL}/events/{gid}", timeout=10)
            if resp.status_code != 200:
                continue
            event = resp.json()
        except Exception:
            continue
        full = event.get("markets") or []
        if len(full) < min_outcomes:
            continue

        tokens = []
        for market in full:
            if market.get("closed") or market.get("archived"):
                continue
            pair = marketdata.extract_tokens(market)
            if pair:
                tokens.append((market, pair[0]))
        if len(tokens) < min_outcomes:
            continue

        books = marketdata.fetch_books_bulk([t for _, t in tokens])
        total = 0.0
        counted = 0
        for market, token in tokens:
            book = books.get(token)
            if book is None:
                continue
            total += book.ask
            counted += 1
        # 结果缺失时合计会偏小, 直接丢弃, 避免制造假机会
        if counted < len(tokens):
            continue
        rows_out.append((total, counted, event.get("title") or ""))

    rows_out.sort()
    print(f"\n可计算的事件组: {len(rows_out)}")
    if not rows_out:
        return 0
    print(f"最小合计 = {rows_out[0][0]:.4f} (结果数 {rows_out[0][1]})")
    for label, cond in (("< 1.000", 1.000), ("< 0.990", 0.990), ("< 0.980", 0.980)):
        print(f"  合计 {label} 的组: {sum(1 for r in rows_out if r[0] < cond)}")
    print("\n最接近的 8 组:")
    for total, count, title in rows_out[:8]:
        print(f"  {total:.4f} ({count} 结果) {str(title)[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 600))
