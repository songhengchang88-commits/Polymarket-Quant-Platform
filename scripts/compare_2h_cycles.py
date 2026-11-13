import json
from datetime import datetime

rounds = []
with open('data/crypto_5m_paper_rounds.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            rounds.append(json.loads(line))

t_v34 = '2026-11-13T18:54:01'
t_v33 = '2026-11-13T17:29:11'

v33_rounds = [r for r in rounds if t_v33 <= r.get('ts', '') < t_v34]
v34_rounds = [r for r in rounds if r.get('ts', '') >= t_v34]

def analyze(rset, name):
    n = len(rset)
    if n == 0:
        print(f"=== {name}: 0 rounds ===")
        return
    wins = sum(1 for r in rset if r.get('realized_pnl_usd', 0) > 0)
    total_pnl = sum(r.get('realized_pnl_usd', 0) for r in rset)
    paired_cnt = sum(1 for r in rset if r.get('paired_shares', 0) > 0)
    unpaired_cnt = n - paired_cnt
    by_coin = {}
    for r in rset:
        c = r.get('coin', 'unknown')
        if c not in by_coin:
            by_coin[c] = {'n': 0, 'wins': 0, 'pnl': 0.0, 'paired': 0, 'cost': 0.0}
        by_coin[c]['n'] += 1
        pnl = r.get('realized_pnl_usd', 0)
        by_coin[c]['pnl'] += pnl
        by_coin[c]['cost'] += r.get('total_cost_usd', 0)
        if pnl > 0:
            by_coin[c]['wins'] += 1
        if r.get('paired_shares', 0) > 0:
            by_coin[c]['paired'] += 1
    
    print(f"=== {name} (共 {n} 轮) ===")
    print(f"胜率: {wins}/{n} ({wins/n*100:.1f}%) | 净盈亏: {total_pnl:+.4f}U")
    print(f"配对达成率: {paired_cnt}/{n} ({paired_cnt/n*100:.1f}%) | 单边暴露: {unpaired_cnt}/{n} ({unpaired_cnt/n*100:.1f}%)")
    for c, stats in by_coin.items():
        cn = stats['n']
        print(f"  - {c.upper():4}: {stats['wins']}/{cn} 胜 ({stats['wins']/cn*100:.1f}%) | 盈亏: {stats['pnl']:+.4f}U | 配对: {stats['paired']}/{cn} ({stats['paired']/cn*100:.1f}%) | 投入: ${stats['cost']:.2f}")

analyze(v33_rounds, "上期 (v3.3 紧急穿盘救险初期 · 17:29~18:54 UTC)")
print()
analyze(v34_rounds, "当期 (v3.4 破除对冲死锁+SOL高波收窄+30U现金硬顶 · 18:54~20:32 UTC)")
