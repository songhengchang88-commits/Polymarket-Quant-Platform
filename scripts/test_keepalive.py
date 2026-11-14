import requests
import time

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0"})

for i in range(3):
    t0 = time.time()
    r = s.get("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", timeout=4)
    data = r.json().get("data", [{}])[0]
    dt = (time.time() - t0) * 1000
    print(f"Attempt {i+1}: status={r.status_code}, time={dt:.0f}ms, last={data.get('last')}")
