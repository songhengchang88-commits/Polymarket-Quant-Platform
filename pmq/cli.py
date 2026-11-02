"""运维 CLI: doctor / scan / status。

设计立场: `scan` 只报告"有多少盘口通过了全部闸门", 不生成任何虚拟成交。
通过率为 0 就是 0 —— 这是判断策略在当前资金规模下是否成立的唯一诚实指标。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, List

from pmq import marketdata, metrics, risk

DEFAULT_JOURNAL = os.path.join("data", "pmq_journal.jsonl")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== PMQ doctor ===")
    ok = True

    has_key = bool(os.getenv("PK") or os.getenv("PRIVATE_KEY"))
    print(f"[{'PASS' if has_key else 'WARN'}] 私钥环境变量 (PK / PRIVATE_KEY): {has_key}")
    ok = ok and has_key

    try:
        import requests

        r = requests.get("https://polymarket.com/api/geoblock", timeout=8)
        data = r.json()
        blocked = data.get("blocked", True)
        print(f"[{'FAIL' if blocked else 'PASS'}] geoblock blocked={blocked} country={data.get('country')} ip={data.get('ip')}")
        ok = ok and not blocked
    except Exception as exc:
        print(f"[FAIL] geoblock 检测失败: {exc}")
        ok = False

    try:
        markets = marketdata.fetch_markets(limit=1)
        print(f"[{'PASS' if markets else 'FAIL'}] gamma 市场接口: {len(markets)} 条")
        ok = ok and bool(markets)
    except Exception as exc:
        print(f"[FAIL] gamma 接口异常: {exc}")
        ok = False

    print("=== doctor:", "OK" if ok else "NOT READY", "===")
    return 0 if ok else 2


def _category(reason: str) -> str:
    """把长原因归到稳定类别, 否则原因分布表会被 leg0/leg1 前缀淹没。"""
    for key in ("盘口非法", "陈旧", "极端价位", "价差", "交叉", "配对成本", "总敞口", "熔断"):
        if key in reason:
            return key
    return reason[:24]


def cmd_scan(args: argparse.Namespace) -> int:
    limits = risk.Limits(
        max_trade_usd=args.budget,
        min_leg_usd=args.min_leg,
        max_pair_cost=args.max_pair_cost,
        max_book_age_s=args.max_age,
    )
    started = time.time()
    markets = marketdata.fetch_markets(limit=args.limit)
    reasons: Counter = Counter()
    passed: List[Dict[str, Any]] = []
    checked = 0

    pairs: List[tuple] = []
    for market in markets:
        pair = marketdata.extract_tokens(market)
        if pair:
            pairs.append((market, pair))

    books = marketdata.fetch_books_bulk([t for _, pair in pairs for t in pair])

    for market, pair in pairs:
        checked += 1
        b0 = books.get(pair[0])
        b1 = books.get(pair[1])
        if b0 is None or b1 is None:
            reasons["盘口缺失"] += 1
            continue
        gate = risk.check_pair(b0, b1, limits, now=time.time())
        if not gate.ok:
            for reason in gate.reasons:
                reasons[_category(reason)] += 1
            continue
        shares, _ = risk.max_shares_for_budget(args.budget, b0.ask, b1.ask, limits)
        if shares <= 0:
            reasons["预算不足/单腿红线"] += 1
            continue
        passed.append({
            "market_id": market.get("id"),
            "question": (market.get("question") or "")[:60],
            "ask_0": b0.ask, "ask_1": b1.ask,
            "pair_cost": round(b0.ask + b1.ask, 4),
            "shares": shares,
            "edge_usd": round(shares * (1 - b0.ask - b1.ask), 4),
        })

    elapsed = time.time() - started
    print(f"扫描 {checked} 个市场, 用时 {elapsed:.1f}s, 通过全部闸门: {len(passed)}")
    print(f"闸门通过率: {(len(passed) / checked * 100 if checked else 0):.2f}%")
    if reasons:
        print("拒绝原因分布:")
        for reason, count in reasons.most_common(10):
            print(f"  - {reason}: {count}")
    if passed:
        print("可执行候选(按锁定边际降序):")
        for row in sorted(passed, key=lambda x: -x["edge_usd"])[:10]:
            print(f"  {row['market_id']} pair={row['pair_cost']:.4f} shares={row['shares']} "
                  f"edge=${row['edge_usd']:.4f} {row['question']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from pmq.journal import Journal

    if not os.path.exists(args.journal):
        print(f"账本不存在: {args.journal}")
        return 1
    events = Journal(args.journal, fsync=False).read_all()
    summary = metrics.summarize(events, args.initial)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    from pmq import detect as detector
    from pmq.fees import CostModel

    limits = risk.Limits(
        max_trade_usd=args.budget,
        min_leg_usd=args.min_leg,
        max_pair_cost=args.max_pair_cost,
        max_book_age_s=args.max_age,
        max_spread_pct=args.max_spread,
    )
    costs = CostModel(taker_fee_bps=args.fee_bps, gas_usd=args.gas)
    started = time.time()

    markets = marketdata.fetch_markets_paged(limit=args.limit)
    # Gamma 会忽略 order 参数, 返回顺序不保证按成交量; 自己排序, 否则过滤阈值会把结果清空
    markets.sort(key=lambda m: -_to_float(m.get("volumeNum") or m.get("volume") or 0))
    if args.min_volume > 0:
        markets = [m for m in markets
                   if _to_float(m.get("volume24hr") or m.get("volumeNum") or 0) >= args.min_volume]

    report = detector.DetectionReport()
    chunk = max(1, args.chunk)
    for start in range(0, len(markets), chunk):
        block = markets[start:start + chunk]
        tokens = []
        for market in block:
            pair = marketdata.extract_tokens(market)
            if pair:
                tokens.extend(pair)
        books = marketdata.fetch_books_bulk(tokens)
        # 取完立刻判断: 陈旧度按"决策时刻 - 拉取时刻"算, 而不是整轮扫描结束时刻
        report.merge(detector.detect(block, books, marketdata.extract_tokens,
                                     limits, costs, args.budget, now=time.time()))
    elapsed = time.time() - started

    print(f"市场数 {report.scanned} | 有盘口 {report.with_books} | 可立即执行 {report.actionable} | 用时 {elapsed:.1f}s")
    print(f"taker 通过率: {(report.actionable / report.with_books * 100 if report.with_books else 0):.2f}%")
    if report.rejected:
        print("拒绝原因:")
        for reason, count in sorted(report.rejected.items(), key=lambda kv: -kv[1]):
            print(f"  - {reason}: {count}")

    actionable = [o for o in report.opportunities if o.executable]
    if actionable:
        print("\n>>> 可执行机会(按边际降序):")
        for opp in sorted(actionable, key=lambda o: -o.edge_usd)[: args.top]:
            print(f"  ${opp.edge_usd:.4f} | {opp.market_id} | pair={opp.pair_cost} | "
                  f"shares={opp.shares} | {opp.question}")
    else:
        print("\n>>> 当前没有可执行的 taker 套利机会。")

    watching = [o for o in report.opportunities if not o.executable and o.kind != detector.TAKER]
    if watching and args.show_watch:
        print(f"\n观察列表(不可直接执行, 共 {len(watching)} 条, 展示前 {args.top}):")
        for opp in sorted(watching, key=lambda o: -o.edge_per_share)[: args.top]:
            print(f"  [{opp.kind}] edge/share={opp.edge_per_share:+.4f} {opp.market_id} {opp.note}")
    return 0


def _find_market(markets: List[Dict[str, Any]], key: str) -> Any:
    for market in markets:
        if str(market.get("id")) == key or market.get("slug") == key:
            return market
    return None


def cmd_probe(args: argparse.Namespace) -> int:
    from pmq.markout import MarkoutProbe

    offsets = [float(x) for x in str(args.offsets).split(",") if x.strip()]
    markets = marketdata.fetch_markets_paged(limit=args.limit)

    if args.market:
        market = _find_market(markets, args.market)
        if market is None:
            print(f"未找到市场: {args.market} (仅在前 {len(markets)} 个市场内搜索)")
            return 2
    else:
        markets.sort(key=lambda m: -_to_float(m.get("volumeNum") or m.get("volume") or 0))
        market = next((m for m in markets if marketdata.extract_tokens(m)), None)
        if market is None:
            print("没有可用于探测的市场")
            return 2

    pair = marketdata.extract_tokens(market)
    if not pair:
        print("该市场缺少 clobTokenIds")
        return 2

    if args.mode == "live":
        if not (os.getenv("POLY_PRIVATE_KEY") or os.getenv("PK")):
            print("live 模式需要 POLY_PRIVATE_KEY / PK, 当前未配置")
            return 2
        print("live 模式尚未在本机验证过, 请先用 dry 模式建立基准")

    print(f"探测市场: {market.get('question')} (id={market.get('id')})")
    print(f"模式: {args.mode} | 采样点: {offsets} 秒 | 基准=当前买一价之和")

    probe = MarkoutProbe(lambda a, b: marketdata.fetch_pair_books(a, b))
    result = probe.run(str(market.get("id")), str(market.get("question", "")),
                       pair[0], pair[1], offsets, mode=args.mode)

    print(f"入场基准: bid_sum={result.entry_bid_sum:.4f} mid_sum={result.entry_mid_sum:.4f}")
    print(f"{'秒':>6} {'bid_sum':>10} {'mid_sum':>10} {'markout%':>10}")
    for s in result.samples:
        print(f"{s.seconds:>6.0f} {s.bid_sum:>10.4f} {s.mid_sum:>10.4f} {s.markout_pct:>10.2f}")
    print(f"\n结论: {result.verdict}")
    print("说明: dry 模式假设在买一价被动成交, 未下任何真实订单, 不产生资金变动。")
    return 0


def cmd_maker(args: argparse.Namespace) -> int:
    from pmq import maker as maker_mod

    cfg = maker_mod.MakerConfig(
        budget_usd=args.budget,
        min_shares=args.min_shares,
        max_spread_cents=args.max_spread,
        min_edge_pct=args.min_edge,
    )
    markets = marketdata.fetch_markets_paged(limit=args.limit)
    picked = maker_mod.select_markets(markets, min_volume=args.min_volume,
                                      max_min_size=args.min_shares, min_days=args.min_days)
    print(f"市场 {len(markets)} 个 -> 满足奖励门槛(${args.budget} 预算)且未到期的 {len(picked)} 个")

    plans = []
    for start in range(0, len(picked), 25):
        block = picked[start:start + 25]
        tokens = []
        pairs = []
        for market in block:
            pair = marketdata.extract_tokens(market)
            if not pair:
                continue
            pairs.append((market, pair))
            tokens.extend(pair)
        books = marketdata.fetch_books_bulk(tokens)
        for market, pair in pairs:
            b_yes = books.get(pair[0])
            b_no = books.get(pair[1])
            if not b_yes or not b_no:
                continue
            plan = maker_mod.plan_quotes(str(market.get("id")), str(market.get("question", ""))[:52],
                                         b_yes, b_no, cfg)
            if plan:
                plans.append(plan)

    if not plans:
        print("没有值得报价的盘口(双边价差不足以覆盖门槛)")
        return 0

    plans.sort(key=lambda p: -p.edge_pct)
    print(f"\n可报价 {len(plans)} 个, 按理论价差降序:")
    print(f"{'edge%':>6} {'mid':>6} {'YES':>6} {'NO':>6} {'sum':>6} {'份数':>6} {'成本$':>8} {'奖励带':>4}  市场")
    for plan in plans[: args.top]:
        band = "Y" if plan.in_band else "n"
        print(f"{plan.edge_pct:>6.2f} {plan.mid_yes:>6.3f} {plan.bid_yes:>6.3f} {plan.bid_no:>6.3f} "
              f"{plan.pair_cost:>6.3f} {plan.shares:>6.1f} {plan.cost_usd:>8.2f} {band:>4}  {plan.question}")
    best = plans[0]
    print(f"\n首选: {best.market_id} | 双边各挂 {best.shares} 份 | 合计 ${best.cost_usd} | "
          f"两腿都成交锁定 ${best.edge_usd} ({best.edge_pct}%)")
    print("注意: 只有一条腿成交即形成裸持, 必须由库存上限与超时平仓兜底。")
    return 0


def cmd_markout(args: argparse.Namespace) -> int:
    from pmq import analyze as analyzer

    horizons = [float(x) for x in str(args.horizons).split(",") if x.strip()]
    rows = analyzer.analyze(args.file, horizons)
    if not rows:
        print(f"样本不足或文件不存在: {args.file}")
        return 1
    print(f"文件: {args.file}")
    print(f"{'horizon':>8} {'samples':>8} {'mean%':>8} {'median%':>9} {'min%':>8} {'max%':>8} {'负占比%':>8}")
    for row in rows:
        print(f"{row['horizon']:>8.0f} {row['samples']:>8d} {row['mean_pct']:>8.3f} "
              f"{row['median_pct']:>9.3f} {row['min_pct']:>8.3f} {row['max_pct']:>8.3f} "
              f"{row['negative_share']:>8.1f}")
    print("\n判读: mean/median 显著为负 => 被动成交后价格继续不利(做市被收割);")
    print("      接近 0 => 噪声, 需要更长采样; 显著为正 => 存在回弹, 做市有真实 edge。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmq", description="Polymarket 量化执行内核 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="环境体检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_scan = sub.add_parser("scan", help="只读扫描, 报告闸门通过率")
    p_scan.add_argument("--limit", type=int, default=50)
    p_scan.add_argument("--budget", type=float, default=3.0)
    p_scan.add_argument("--min-leg", type=float, default=1.15)
    p_scan.add_argument("--max-pair-cost", type=float, default=0.985)
    p_scan.add_argument("--max-age", type=float, default=5.0)
    p_scan.set_defaults(func=cmd_scan)

    p_detect = sub.add_parser("detect", help="全市场机会检测(只读, 不下单)")
    p_detect.add_argument("--limit", type=int, default=300)
    p_detect.add_argument("--min-volume", type=float, default=0.0)
    p_detect.add_argument("--budget", type=float, default=3.0)
    p_detect.add_argument("--min-leg", type=float, default=1.15)
    p_detect.add_argument("--max-pair-cost", type=float, default=0.985)
    p_detect.add_argument("--max-age", type=float, default=5.0)
    p_detect.add_argument("--max-spread", type=float, default=0.05)
    p_detect.add_argument("--fee-bps", type=float, default=0.0)
    p_detect.add_argument("--gas", type=float, default=0.002)
    p_detect.add_argument("--top", type=int, default=15)
    p_detect.add_argument("--chunk", type=int, default=50)
    p_detect.add_argument("--show-watch", action="store_true")
    p_detect.set_defaults(func=cmd_detect)

    p_probe = sub.add_parser("probe", help="markout/逆选择探测(默认 dry, 不下单)")
    p_probe.add_argument("--market", default="", help="市场 id 或 slug, 缺省则取成交量最高者")
    p_probe.add_argument("--offsets", default="60,300,900", help="采样秒数, 逗号分隔")
    p_probe.add_argument("--mode", choices=["dry", "live"], default="dry")
    p_probe.add_argument("--limit", type=int, default=300)
    p_probe.set_defaults(func=cmd_probe)

    p_maker = sub.add_parser("maker", help="做市报价规划(只算不挂)")
    p_maker.add_argument("--budget", type=float, default=20.0)
    p_maker.add_argument("--min-shares", type=float, default=20.0)
    p_maker.add_argument("--max-spread", type=float, default=4.5, help="奖励带半宽(美分)")
    p_maker.add_argument("--min-edge", type=float, default=0.3, help="最低理论价差(%)")
    p_maker.add_argument("--min-volume", type=float, default=5000.0)
    p_maker.add_argument("--min-days", type=float, default=7.0)
    p_maker.add_argument("--limit", type=int, default=400)
    p_maker.add_argument("--top", type=int, default=12)
    p_maker.set_defaults(func=cmd_maker)

    p_status = sub.add_parser("status", help="按账本汇总绩效")
    p_status.add_argument("--journal", default=DEFAULT_JOURNAL)
    p_status.add_argument("--initial", type=float, default=36.91)
    p_status.set_defaults(func=cmd_status)

    p_markout = sub.add_parser("markout", help="从采集文件计算逆选择(markout)")
    p_markout.add_argument("--file", default=os.path.join("data", "markout_samples.jsonl"))
    p_markout.add_argument("--horizons", default="60,180,300")
    p_markout.set_defaults(func=cmd_markout)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
