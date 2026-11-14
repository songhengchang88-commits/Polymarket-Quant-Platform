import json
import time
from datetime import datetime, timezone, timedelta

# 统一采用北京时间 (UTC+8) 显示
BEIJING_TZ = timezone(timedelta(hours=8))

rounds = []
with open('data/crypto_5m_paper_rounds.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            try:
                rounds.append(json.loads(line))
            except Exception:
                pass

now = time.time()
t_now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
t_2h_ago_utc = datetime.fromtimestamp(now - 7200.0, tz=timezone.utc)
t_4h_ago_utc = datetime.fromtimestamp(now - 14400.0, tz=timezone.utc)

t_now_bj = t_now_utc.astimezone(BEIJING_TZ)
t_2h_ago_bj = t_2h_ago_utc.astimezone(BEIJING_TZ)
t_4h_ago_bj = t_4h_ago_utc.astimezone(BEIJING_TZ)

curr_rounds = []
prev_rounds = []

for r in rounds:
    ts_str = r.get('ts')
    if not ts_str:
        continue
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt >= t_2h_ago_utc:
            curr_rounds.append(r)
        elif t_4h_ago_utc <= dt < t_2h_ago_utc:
            prev_rounds.append(r)
    except Exception:
        pass

def analyze(rset, name):
    n = len(rset)
    if n == 0:
        print(f"=== {name}: 0 轮数据 ===")
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

analyze(prev_rounds, f"上期 2小时周期 ({t_4h_ago_bj.strftime('%H:%M')} ~ {t_2h_ago_bj.strftime('%H:%M')} 北京时间)")
print()
analyze(curr_rounds, f"当期 2小时周期 ({t_2h_ago_bj.strftime('%H:%M')} ~ {t_now_bj.strftime('%H:%M')} 北京时间)")
