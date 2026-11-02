import urllib.request
import json
import time
import sys

sys.stdout.reconfigure(encoding='utf-8')

time.sleep(2) # 等待启动

# 1. 触发一次实战跳价演练
req_drill = urllib.request.Request("http://127.0.0.1:8888/api/drill", data=b"", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req_drill) as resp:
    drill_res = json.loads(resp.read().decode('utf-8'))
    print("=== [演练触发结果] ===")
    print(f"Status: {drill_res.get('status')}")
    pos = drill_res.get('position')
    if pos:
        print(f"成功建仓: {pos.get('question')}")
        print(f"投入本金: {pos.get('invested_usd')} U | 锁定纯利: +{pos.get('expected_profit_usd')} U (+{pos.get('profit_pct')}%)")

# 2. 查询当前账户状态
req_status = urllib.request.Request("http://127.0.0.1:8888/api/status")
with urllib.request.urlopen(req_status) as resp:
    data = json.loads(resp.read().decode('utf-8'))
    acc = data.get("account", {})
    print("\n=== [40U 专研账户最新仪表] ===")
    print(f"可用现金: {acc.get('current_cash')} U")
    print(f"执行中套利持仓: {acc.get('invested_in_bags')} U (共 {acc.get('active_positions_count')} 个袋)")
    print(f"总净资产: {acc.get('total_equity')} U")
    print(f"监控市场总数: {len(data.get('markets', []))} 个")
    print(f"扫描轮询计数: {data.get('scan_count')} 次")
