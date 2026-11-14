import json

rounds = []
with open('data/crypto_5m_paper_rounds.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            rounds.append(json.loads(line))

t_v34 = '2026-11-13T18:54:01'
v34_rounds = [r for r in rounds if r.get('ts', '') >= t_v34]
print(f"Total rounds in v3.4/v3.5: {len(v34_rounds)}")

wins = sum(1 for r in v34_rounds if r.get('realized_pnl_usd', 0) > 0)
total_pnl = sum(r.get('realized_pnl_usd', 0) for r in v34_rounds)
paired_cnt = sum(1 for r in v34_rounds if r.get('paired_shares', 0) > 0)

print(f"综合胜率: {wins}/{len(v34_rounds)} ({wins/len(v34_rounds)*100:.1f}%)")
print(f"双边配对率: {paired_cnt}/{len(v34_rounds)} ({paired_cnt/len(v34_rounds)*100:.1f}%)")
print(f"单边暴露率: {len(v34_rounds)-paired_cnt}/{len(v34_rounds)} ({(len(v34_rounds)-paired_cnt)/len(v34_rounds)*100:.1f}%)")
print(f"总净盈亏: {total_pnl:+.4f}U")

by_coin = {}
for r in v34_rounds:
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

for c, s in by_coin.items():
    print(f"  - {c.upper()}: {s['wins']}/{s['n']} 胜 ({s['wins']/s['n']*100:.1f}%) | 盈亏: {s['pnl']:+.4f}U | 配对: {s['paired']}/{s['n']} ({s['paired']/s['n']*100:.1f}%) | 投入: ${s['cost']:.2f}")
