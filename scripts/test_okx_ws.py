import asyncio
import json
import websockets

async def test_okx():
    url = "wss://ws.okx.com:8443/ws/v5/public"
    try:
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
            for _ in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                d = json.loads(msg)
                if "data" in d:
                    ticker = d["data"][0]
                    print(f"OKX WS: {ticker['instId']} last={ticker.get('last')} ask={ticker.get('askPx')} bid={ticker.get('bidPx')}")
            print("OKX WS SUCCESS!")
    except Exception as e:
        print(f"OKX WS FAILED: {e}")

asyncio.run(test_okx())
