"""网络代理环境变量清洗 (netclean)。

故障现象: 本机 / CI 的 NO_PROXY 常被 VPN 客户端或终端追加 IPv6 条目 (如 `::1`、`[::1]`)。
httpx 的 `get_environment_proxies()` 会把它们转成挂载键 `all://[::1]`,
而 `URLPattern` 解析端口时直接抛 `InvalidURL: Invalid port: ':1]'`,
于是 `httpx.Client()` —— 以及任何在模块级构造 httpx 客户端的库
(py_clob_client 与 py_clob_client_v2 的 http_helpers 都是 `httpx.Client(http2=True)`)——
一 import 就崩, 实盘链路的凭证派生 / 下单全部不可用。

官方 py_clob_client_v2 也为此自带了一段 NO_PROXY 清洗补丁, 但 py_clob_client(V1) 没有。
本项目统一在 import httpx / SDK 之前调用 `sanitize_proxy_env()`, 从根上消除该崩溃。
"""
from __future__ import annotations

import os

# httpx 无法安全解析的 IPv6 通配写法: 冒号通配与方括号地址
_BAD_SUBSTRINGS = ("::", "[")

_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def sanitize_proxy_env(verbose: bool = False) -> bool:
    """移除 NO_PROXY / no_proxy 中 httpx 无法解析的条目, 返回是否发生过修改。

    只做"删除无法解析的条目"这一件事, 绝不改写可用的代理配置,
    以免破坏用户真实的代理链路。
    """
    changed = False
    for key in _PROXY_KEYS:
        raw = os.environ.get(key)
        if not raw:
            continue
        kept = [p.strip() for p in raw.split(",")
                if p.strip() and not any(s in p for s in _BAD_SUBSTRINGS)]
        cleaned = ",".join(kept)
        if cleaned != raw:
            os.environ[key] = cleaned
            if verbose:
                print(f"[netclean] {key}: {raw!r} -> {cleaned!r}")
            changed = True
    return changed


# 导入即清洗, 保证任何 pmq.* 子模块 (乃至随后被 import 的第三方 httpx 客户端) 拿到干净环境
sanitize_proxy_env(verbose=bool(os.environ.get("PMQ_NETCLEAN_VERBOSE")))
