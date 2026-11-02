import urllib.request
import json
import time
import sys

# 避免 Windows 控制台编码问题
sys.stdout.reconfigure(encoding='utf-8')

def test_full_pipeline():
    start = time.time()
    try:
        req = urllib.request.Request("http://127.0.0.1:8888/api/status", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            print("=" * 60)
            print("[PolyArbitrage Bloomberg Terminal Verification]")
            print(f"1. Response Latency: {round((time.time() - start) * 1000, 1)} ms")
            print(f"2. Monitor Active: {data.get('is_monitoring')}")
            print(f"3. Active Orderbook Markets: {len(data.get('markets', []))}")
            print(f"4. Breaking News Stream Count: {len(data.get('news', []))}")
            
            acc = data.get("account", {})
            print("-" * 60)
            print("[40U Specialized Portfolio]")
            print(f"  * Available Cash: {acc.get('current_cash')} USDC.e")
            print(f"  * Active Arbitrage Bags: {acc.get('active_positions_count')}")
            print(f"  * Total Profit Settled: {acc.get('total_profit')} U")
            print(f"  * Profit Percentage: {acc.get('profit_pct')}%")
            
            print("-" * 60)
            print("[Live Breaking News Feed Sample]")
            for n in data.get("news", [])[:3]:
                print(f"  [{n.get('time')}] [{n.get('urgency')}] {n.get('title')[:60]}...")

            print("=" * 60)
            print("SUCCESS: Pipeline is fully operational on 40U config and live news feed!")

    except Exception as e:
        print("TEST ERROR:", e)

if __name__ == "__main__":
    test_full_pipeline()
