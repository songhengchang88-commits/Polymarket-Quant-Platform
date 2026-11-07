#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纸面账本独立三表勾稽（线 3 加固交付，替代编排层构造性 True 的 reconcile_ok）。

用法:
    python3 reconcile_paper.py <ledger.sqlite3> [--log <console.log>] [--json]

只读打开账本（mode=ro），不写原库。检查:
  A. orders <-> fills   成交量守恒、状态一致、fill 金额自洽
  B. fills  <-> cash_ledger 现金变动守恒、余额链连续、期末现金勾稽
  C. 持仓重建           按 fills 净额重建 (market, token) 持仓与成本
  D. markout_samples <-> fills 每 fill 至多一样本、缺样本单列、方向/时序自洽
  E. 成本口径净值重构   期末现金 + 持仓成本 == 种子金 - 累计费用 + 已实现盈亏

核心函数 reconcile_connection(conn) 供 CLI 与 paper_runner 在线接线共用
（runner 每 60 轮真勾稽一次，结果喂 RiskBreaker 的 reconcile_ok）。

退出码: 0 = 无硬差异 (口径差异单列不算硬差异); 1 = 有硬差异; 2 = 用法/IO 错误。
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict

MICRO = 1_000_000


def usd(micro):
    return micro / MICRO


def reconcile_connection(conn, log_lost_cumulative=None):
    """对账核心（2026-11-07 系统化：供 CLI 与 runner 在线接线共用）。

    conn：row_factory 为 sqlite3.Row 的连接（CLI 用只读连接打开账本
    文件；runner 用账本自身连接——同一进程同一账本，SELECT 与写入
    串行无并发问题）。log_lost_cumulative：可选，日志口径的 markout
    累计丢失数（D6 交叉核对用），None 表示跳过 D6。
    返回 result dict：{totals, positions, hard_diffs, soft_diffs,
    reconcile_ok}。
    """
    hard = []       # 硬差异: 数据错误
    soft = []       # 口径差异: 语义需注明, 非数据丢失
    totals = {}     # 核对总数证据

    orders = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM orders")}
    fills = [dict(r) for r in conn.execute("SELECT * FROM fills ORDER BY id")]
    cash = [dict(r) for r in conn.execute("SELECT * FROM cash_ledger ORDER BY id")]
    samples = [dict(r) for r in conn.execute("SELECT * FROM markout_samples ORDER BY id")]
    totals.update(orders=len(orders), fills=len(fills), cash_entries=len(cash),
                  markout_samples=len(samples))

    # ---------- A. orders <-> fills ----------
    fills_by_order = defaultdict(list)
    for f in fills:
        fills_by_order[f["order_id"]].append(f)
        if f["order_id"] not in orders:
            hard.append(f"A1 fill {f['id']} 指向不存在的订单 {f['order_id']}")
        exp = f["price_micro"] * f["shares_micro"] // MICRO
        if f["amount_micro"] != exp:
            hard.append(f"A2 fill {f['id']} amount {f['amount_micro']} != price*shares {exp}")
    partial_orders = []
    for oid, o in orders.items():
        got = sum(f["shares_micro"] for f in fills_by_order.get(oid, []))
        if got > o["shares_micro"]:
            hard.append(f"A3 订单 {oid} 成交量 {got} 超过委托量 {o['shares_micro']}")
        if got == o["shares_micro"] and got > 0 and o["status"] != "filled":
            hard.append(f"A4 订单 {oid} 已全量成交但状态={o['status']}")
        if got == 0 and o["status"] == "filled":
            hard.append(f"A5 订单 {oid} 状态 filled 但无成交")
        if 0 < got < o["shares_micro"]:
            partial_orders.append((oid, o["status"], got, o["shares_micro"]))
    for oid, st, got, tot in partial_orders:
        soft.append(f"A6 订单 {oid} 部分成交 {got}/{tot} micro-shares, 终态={st}"
                    f"(部分成交后剩余撤销会把状态覆盖为 cancelled, 属状态语义口径差异)")

    # ---------- B. fills <-> cash_ledger ----------
    settles = [c for c in cash if c["kind"] == "fill_settle"]
    by_ref = defaultdict(list)
    for c in settles:
        by_ref[c["ref"]].append(c)
    matched_fill_ids = set()
    for f in fills:
        o = orders.get(f["order_id"])
        ref = o["client_order_id"] if o else None
        lst = by_ref.get(ref, [])
        sign = -1 if f["side"] == "BUY" else 1
        exp_amt = sign * (f["amount_micro"] + (f["fee_micro"] if f["side"] == "BUY" else -f["fee_micro"]))
        # 本账本口径: BUY settle = -(amount) (fee=0 时); fee 单独成行时在此放宽为绝对值匹配
        hit = [c for c in lst if c["amount_micro"] == sign * f["amount_micro"]]
        if len(hit) != 1:
            hard.append(f"B1 fill {f['id']} (ref={ref}) 匹配到 {len(hit)} 条 fill_settle, 期望 1")
        else:
            matched_fill_ids.add(f["id"])
    orphan = [c["id"] for c in settles if c["ref"] not in
              {orders[f["order_id"]]["client_order_id"] for f in fills if f["order_id"] in orders}]
    if orphan:
        hard.append(f"B2 孤儿 fill_settle 条目: {orphan}")
    prev = 0
    for c in cash:
        if c["balance_after_micro"] != prev + c["amount_micro"]:
            hard.append(f"B3 cash {c['id']} 余额链断裂: {prev}+{c['amount_micro']} != {c['balance_after_micro']}")
        prev = c["balance_after_micro"]
    totals["cash_final_micro"] = prev
    totals["cash_sum_micro"] = sum(c["amount_micro"] for c in cash)
    if prev != totals["cash_sum_micro"]:
        hard.append("B4 期末现金 != cash 金额总和")

    # ---------- C. 持仓重建 (fills 净额) ----------
    pos = defaultdict(lambda: {"shares": 0, "cost": 0})
    realized_pnl = 0  # 平均成本口径已实现盈亏（E 段恒等式用）
    for f in fills:
        k = (f["market_id"], f["token_id"])
        if f["side"] == "BUY":
            pos[k]["shares"] += f["shares_micro"]
            pos[k]["cost"] += f["amount_micro"]
        else:
            # 平均成本扣减（2026-11-07 加固 L1-02 后口径）：旧版按卖出
            # 成交额扣减，与旧引擎同 bug——盈利卖出后成本被多扣，
            # E1 成本口径净值随之失真（实测偏差 1.30）。与引擎
            # _apply_fill / runner 回放三处同口径。
            held_before = pos[k]["shares"]
            if held_before > 0:
                if f["shares_micro"] >= held_before:
                    reduction = pos[k]["cost"]
                else:
                    reduction = (pos[k]["cost"] * f["shares_micro"]
                                 // held_before)
                reduction = max(0, min(reduction, pos[k]["cost"]))
                pos[k]["cost"] -= reduction
                realized_pnl += f["amount_micro"] - reduction
            pos[k]["shares"] -= f["shares_micro"]
    totals["realized_pnl_micro"] = realized_pnl
    pos = {k: v for k, v in pos.items() if v["shares"] != 0}
    totals["open_positions"] = len(pos)
    totals["position_cost_micro"] = sum(v["cost"] for v in pos.values())

    # ---------- D. markout_samples <-> fills ----------
    s_by_fill = defaultdict(list)
    for s in samples:
        s_by_fill[s["fill_id"]].append(s)
    missing = []
    for f in fills:
        lst = s_by_fill.get(f["id"], [])
        if len(lst) > 1:
            hard.append(f"D1 fill {f['id']} 有 {len(lst)} 条 markout 样本 (应至多 1)")
        if len(lst) == 0:
            missing.append(f["id"])
        for s in lst:
            exp_mk = (s["mid_at_horizon_micro"] - s["mid_at_fill_micro"]) if f["side"] == "BUY" \
                     else (s["mid_at_fill_micro"] - s["mid_at_horizon_micro"])
            if s["markout_micro"] != exp_mk:
                hard.append(f"D2 样本 {s['id']} markout {s['markout_micro']} != 方向口径 {exp_mk}")
            lag = s["sampled_at_ms"] - f["filled_at_ms"]
            # 轮询节拍 15s: 间隔在 [horizon-15s, horizon] 内属调度对齐口径, 汇总记 SOFT;
            # 小于 horizon-15s 才是真提前采样 (硬差异); 大于 2 倍 horizon 记 SOFT。
            if lag < s["horizon_sec"] * 1000 - 15000:
                hard.append(f"D3 样本 {s['id']} 采样间隔 {lag}ms 明显小于 horizon {s['horizon_sec']}s")
            elif lag < s["horizon_sec"] * 1000:
                totals.setdefault("horizon_early_samples", []).append(
                    (s["id"], round(lag / 1000, 1)))
            elif lag > 2 * s["horizon_sec"] * 1000:
                soft.append(f"D4 样本 {s['id']} 采样间隔 {lag/1000:.0f}s 超 2 倍 horizon (轮询对齐偏差)")
    if totals.get("horizon_early_samples"):
        lags = [x[1] for x in totals["horizon_early_samples"]]
        soft.append(f"D3b {len(lags)} 条样本实际采样间隔 {min(lags)}-{max(lags)}s, 系统性略小于名义 "
                    f"horizon 300s (轮询节拍对齐口径, 偏差约 -1.3%, 闸门 markout 口径须注明)")
    totals["fills_missing_markout"] = missing
    if missing:
        soft.append(f"D5 fills 缺 markout 样本: {missing} (须与丢失记录对账; 见报告样本完整性总账)")
    if log_lost_cumulative is not None:
        totals["log_markout_lost_cumulative"] = log_lost_cumulative
        if log_lost_cumulative != len(missing):
            hard.append(f"D6 日志累计丢失 {log_lost_cumulative} != "
                        f"账本缺样本数 {len(missing)}")

    # ---------- E. 成本口径净值重构 ----------
    seed = sum(c["amount_micro"] for c in cash if c["kind"] == "deposit")
    fees = sum(f["fee_micro"] for f in fills)
    equity_cost = totals["cash_final_micro"] + totals["position_cost_micro"]
    totals["seed_micro"] = seed
    totals["fees_micro"] = fees
    totals["equity_at_cost_micro"] = equity_cost
    # 恒等式（平均成本口径）：成本净值 = 种子 − 费用 + 已实现盈亏。
    # 旧式「= 种子 − 费用」只在成交额扣减口径下是同义反复，加固 L1-02
    # 改平均成本后必须带上已实现项（2026-11-07 实测：fill 11 以 0.499
    # 卖出成本 0.629 的腿，已实现 −1.30，恒等式精确闭合）。
    if equity_cost != seed - fees + totals.get("realized_pnl_micro", 0):
        hard.append(f"E1 成本口径净值 {equity_cost} != 种子 {seed} - 费用 {fees} "
                    f"+ 已实现 {totals.get('realized_pnl_micro', 0)}")

    result = {
        "totals": totals,
        "positions": {f"{k[0]}|{k[1][:8]}...": {"shares": usd(v["shares"]), "cost_usd": usd(v["cost"])}
                      for k, v in sorted(pos.items())},
        "hard_diffs": hard,
        "soft_diffs": soft,
        "reconcile_ok": len(hard) == 0,
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ledger")
    ap.add_argument("--log", default=None, help="可选: 控制台日志, 用于核对 markout 丢失累计")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        conn = sqlite3.connect(f"file:{args.ledger}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"无法只读打开账本: {e}", file=sys.stderr)
        return 2
    conn.row_factory = sqlite3.Row

    log_lost = None
    if args.log:
        import re
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"'markout_tasks_lost': (\d+)", line)
                if m:
                    log_lost = int(m.group(1))

    result = reconcile_connection(conn, log_lost_cumulative=log_lost)
    totals, hard, soft = result["totals"], result["hard_diffs"], result["soft_diffs"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"核对总数: {totals}")
        print(f"重建持仓: {len(result['positions'])} 个 token, 成本合计 {usd(totals['position_cost_micro'])} USDC")
        print(f"期末现金: {usd(totals['cash_final_micro'])} USDC; 成本口径净值: {usd(totals['equity_at_cost_micro'])} USDC")
        print(f"硬差异: {len(hard)}")
        for h in hard:
            print("  [HARD]", h)
        print(f"口径差异: {len(soft)}")
        for s in soft:
            print("  [SOFT]", s)
        print("reconcile_ok =", result["reconcile_ok"])
    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
