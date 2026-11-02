"""实盘连通性测试(livetest): 深价 post-only 挂单 -> 立即撤单。

不产生成交、不占用资金, 但会真实经过 签名 -> 提交 -> 交易所受理 -> 撤单 全链路。
输出全部为交易所原始回执, 不做任何美化。
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)

from py_clob_client_v2 import clob_types as T
from py_clob_client_v2.client import ClobClient

HOST = "https://clob.polymarket.com"


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    price = float(sys.argv[2]) if len(sys.argv) > 2 else 0.01
    size = float(sys.argv[3]) if len(sys.argv) > 3 else 100.0
    if not token:
        print("usage: livetest.py <token_id> [price] [size]")
        return 2

    key = os.getenv("POLY_PRIVATE_KEY", "")
    funder = os.getenv("FUNDER_ADDRESS", "")
    sig = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
    if not key:
        print("缺少 POLY_PRIVATE_KEY")
        return 2

    client = ClobClient(host=HOST, chain_id=137, key=key, signature_type=sig,
                        funder=funder if sig != 0 else None,
                        retry_on_error=True, use_server_time=True)
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)

    bal = client.get_balance_allowance(
        params=T.BalanceAllowanceParams(asset_type=T.AssetType.COLLATERAL))
    print(f"balance_usdc={int(bal.get('balance', '0')) / 1e6:.4f}")

    tick = client.get_tick_size(token)
    print(f"tick_size={tick}")

    args_cls = getattr(T, "OrderArgsV2", T.OrderArgs)
    order_args = args_cls(token_id=token, price=price, size=size, side="BUY")
    signed = client.create_order(order_args)
    started = time.time()
    resp = client.post_order(signed, T.OrderType.GTC, post_only=True)
    elapsed = (time.time() - started) * 1000
    print(f"post_ms={elapsed:.0f}")
    print(f"post_resp={resp}")

    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")

    if order_id:
        cancel_method = None
        for name in ("cancel_orders", "cancel_order", "cancel"):
            if hasattr(client, name):
                cancel_method = getattr(client, name)
                break
        if cancel_method is None:
            print("未找到撤单方法, 请手动到前端撤销 orderID=" + str(order_id))
            return 3
        try:
            if cancel_method.__name__ == "cancel_orders":
                out = cancel_method([order_id])
            else:
                out = cancel_method(order_id)
            print(f"cancel_resp={out}")
        except Exception as exc:
            print(f"cancel_failed={type(exc).__name__}: {exc} orderID={order_id}")
            return 3
    else:
        print("未拿到 orderID, 无法自动撤单")
        return 3

    print("LIVETEST_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
