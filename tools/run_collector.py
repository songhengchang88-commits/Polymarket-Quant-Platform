"""持续采集盘口快照, 供 markout 分析使用。零交易、零资金变动。

用法:
    python tools/run_collector.py --minutes 30 --interval 30 --markets 6
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmq import marketdata
from pmq.collect import Collector
from pmq.maker import select_markets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--markets", type=int, default=6)
    parser.add_argument("--max-days", type=float, default=9999.0, help="只采集剩余到期小于该天数的市场")
    parser.add_argument("--out", default=os.path.join("data", "markout_samples.jsonl"))
    args = parser.parse_args()

    universe = marketdata.fetch_markets_paged(limit=400)
    picked = select_markets(universe, min_volume=5000, max_min_size=20,
                            min_days=0.0, max_days=args.max_days)
    picked.sort(key=lambda m: -(float(m.get("volume24hr") or 0)))
    picked = picked[: args.markets]
    if not picked:
        print("没有满足条件的市场")
        return 1

    print(f"采集市场 {len(picked)} 个, 间隔 {args.interval}s, 时长 {args.minutes} 分钟")
    for m in picked:
        print("  -", m.get("id"), str(m.get("question"))[:60])

    collector = Collector(picked, args.out)
    deadline = time.time() + args.minutes * 60
    rounds = 0
    while time.time() < deadline:
        started = time.time()
        written = collector.sample()
        rounds += 1
        print(f"  [{time.strftime('%H:%M:%S')}] round {rounds}: {written} 条", flush=True)
        sleep_for = max(0.0, args.interval - (time.time() - started))
        if time.time() + sleep_for > deadline:
            break
        time.sleep(sleep_for)

    print(f"完成: {rounds} 轮, 文件 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
