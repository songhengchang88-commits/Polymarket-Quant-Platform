import json, os, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
sys.stdout.reconfigure(encoding='utf-8')

local_now = datetime.now()
print(f'=== 0. TIME BENCHMARK ===')
print(f'Report Generated At: {local_now.strftime("%Y-%m-%d %H:%M:%S")} (Local Time / UTC+8)')
print(f'Next Inspection Due: {(local_now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")} (Local Time / UTC+8)')

print('\n=== 1. SUMMARY ===')
if os.path.exists('data/crypto_5m_paper_summary.json'):
    with open('data/crypto_5m_paper_summary.json', 'r', encoding='utf-8') as f:
        print(f.read())

print('\n=== 2. LIVE STATE SNAPSHOT ===')
if os.path.exists('data/crypto_5m_live_state.json'):
    with open('data/crypto_5m_live_state.json', 'r', encoding='utf-8') as f:
        st = json.load(f)
        print(f"Latency: {st.get('latency_ms')} ms | Paper Cash: ${st.get('paper_cash_usd')} | Equity: ${st.get('current_equity_usd')}")

print('\n=== 3. ROUNDS ANALYSIS ===')
rounds = []
if os.path.exists('data/crypto_5m_paper_rounds.jsonl'):
    with open('data/crypto_5m_paper_rounds.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                rounds.append(json.loads(line))
print(f'Total settled rounds: {len(rounds)}')

by_coin = defaultdict(lambda: {'n': 0, 'win': 0, 'pnl': 0.0, 'paired_rounds': 0, 'cost': 0.0})
paired_pnl = 0.0
paired_cnt = 0
unpaired_pnl = 0.0
unpaired_cnt = 0
by_source = defaultdict(int)

for r in rounds:
    c = r['coin']
    pnl = r['realized_pnl_usd']
    by_source[r.get('source', 'unknown')] += 1
    by_coin[c]['n'] += 1
    by_coin[c]['pnl'] += pnl
    by_coin[c]['cost'] += r['total_cost_usd']
    if pnl > 0:
        by_coin[c]['win'] += 1
    if r.get('paired_shares', 0) > 0:
        by_coin[c]['paired_rounds'] += 1
        paired_cnt += 1
        paired_pnl += pnl
    else:
        unpaired_cnt += 1
        unpaired_pnl += pnl

for c, st in by_coin.items():
    wr = (st['win'] / st['n'] * 100) if st['n'] else 0
    print(f"Coin {c.upper()}: rounds={st['n']}, win={st['win']} ({wr:.1f}%), paired={st['paired_rounds']}, cost=${st['cost']:.2f}, pnl={st['pnl']:+.4f}U")

print(f"Settlement Sources: {dict(by_source)}")
print(f"Paired rounds: {paired_cnt} -> PnL={paired_pnl:+.4f}U (avg={paired_pnl/max(1,paired_cnt):+.4f}U)")
print(f"Unpaired (single-leg) rounds: {unpaired_cnt} -> PnL={unpaired_pnl:+.4f}U (avg={unpaired_pnl/max(1,unpaired_cnt):+.4f}U)")

print('\n--- Last 15 Rounds ---')
for r in rounds[-15:]:
    print(f"{r['slug']} | {r['source']} | Win:{r['winner']} | UP:{r['up_shares']:.1f}(${r['up_cost']:.2f}) DN:{r['down_shares']:.1f}(${r['down_cost']:.2f}) | Paired:{r['paired_shares']:.1f} | PnL:{r['realized_pnl_usd']:+.4f}U | Eq:${r['equity_usd']:.2f}")

print('\n=== 4. TRADES ANALYSIS ===')
trades = []
if os.path.exists('data/crypto_5m_paper_trades.jsonl'):
    with open('data/crypto_5m_paper_trades.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                trades.append(json.loads(line))
print(f'Total fills: {len(trades)}')
by_tag = defaultdict(lambda: {'n': 0, 'cost': 0.0, 'shares': 0.0})
for t in trades:
    tag = t.get('tag', 'unknown')
    by_tag[tag]['n'] += 1
    by_tag[tag]['cost'] += t.get('cost_usd', 0.0)
    by_tag[tag]['shares'] += t.get('shares', 0.0)
for tag, st in by_tag.items():
    print(f"Tag {tag}: fills={st['n']}, total_cost=${st['cost']:.2f}, avg_price=${st['cost']/max(1e-9,st['shares']):.3f}")

print('\n=== 5. LIVE READINESS & RECOMMENDATION ===')
total_r = len(rounds)
pair_rate = (paired_cnt / max(1, total_r)) * 100
win_cnt = sum(1 for r in rounds if r['realized_pnl_usd'] > 0)
wr = (win_cnt / max(1, total_r)) * 100

print(f"Total Evaluated Rounds: {total_r}")
print(f"Overall Win Rate: {wr:.2f}% (Threshold: >=65.0%) -> {'PASS' if wr >= 65.0 else 'FAIL'}")
print(f"Pairing Success Rate: {pair_rate:.2f}% (Threshold: >=60.0%) -> {'PASS' if pair_rate >= 60.0 else 'FAIL'}")
print(f"Net Realized PnL: {paired_pnl + unpaired_pnl:+.4f}U -> {'PASS' if (paired_pnl + unpaired_pnl) > 0 else 'FAIL'}")
print(f"Pairing Profit Coverage: paired_pnl(${paired_pnl:.2f}) vs unpaired_loss(${abs(unpaired_pnl):.2f})")

if wr >= 65.0 and pair_rate >= 60.0 and (paired_pnl + unpaired_pnl) > 0:
    print("RECOMMENDATION: [READY_FOR_MICRO_PILOT] 满足小资金微试水条件 (建议 $30~$50 USDC 极轻仓启动)")
else:
    print("RECOMMENDATION: [CONTINUE_PAPER_OBSERVATION] 建议继续纸面观察至少 1 个周期，重点检验极端行情抗摔性")
