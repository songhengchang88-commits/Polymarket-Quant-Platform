import concurrent.futures
import time
import requests

session = requests.Session()

def fetch_url(url):
    t0 = time.time()
    try:
        r = session.get(url, timeout=3.5)
        dt = (time.time() - t0) * 1000
        return url, r.status_code, dt, r.json()
    except Exception as e:
        dt = (time.time() - t0) * 1000
        return url, -1, dt, str(e)

urls = [
    "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
]

t0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_url, u) for u in urls]
    for f in concurrent.futures.as_completed(futures):
        url, code, dt, res = f.result()
        domain = url.split("/")[2]
        print(f"Parallel result from {domain}: code={code}, time={dt:.0f}ms")
print(f"Total time for first response: {(time.time()-t0)*1000:.0f}ms")
