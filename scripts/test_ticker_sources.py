import requests
import time

urls = [
    "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api.binance.us/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
    "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
]

for u in urls:
    t0 = time.time()
    domain = u.split("/")[2]
    try:
        r = requests.get(u, timeout=3)
        dt = (time.time() - t0) * 1000
        print(f"{domain:<25}: status={r.status_code}, time={dt:.0f}ms")
    except Exception as e:
        print(f"{domain:<25}: FAILED ({e})")
