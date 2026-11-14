import asyncio
import json
import websockets

async def test_all_coins():
    url = "wss://ws.okx.com:8443/ws/v5/public"
    async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
        sub = {
            "op": "subscribe",
            "args": [
                {"channel": "tickers", "instId": "BTC-USDT"},
                {"channel": "tickers", "instId": "ETH-USDT"},
                {"channel": "tickers", "instId": "SOL-USDT"},
            ]
        }
        await ws.send(json.dumps(sub))
        seen = set()
        for _ in range(15):
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            d = json.loads(msg)
            if "data" in d:
                t = d["data"][0]
                inst = t["instId"]
                seen.add(inst)
                print(f"OKX: {inst:<10} price={t['last']}")
            if len(seen) == 3:
                break
        print(f"All 3 coins streamed successfully: {seen}")

asyncio.run(test_all_coins())
