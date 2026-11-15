"""Polymarket 量化执行内核。

设计原则(来自开源项目复盘):
1. 策略是纯函数, I/O 全部隔离 -> 可离线单测, 不依赖实盘网络;
2. 成交以回执为唯一真相, 拒绝"模型利润"入账;
3. 双腿必须原子化(FOK/FAK), 单腿失败立即补偿或标记未对冲;
4. 所有成本(手续费/Gas/点差)显式入账。
"""

__all__ = ["journal", "fees", "risk", "metrics", "execution", "netclean"]
__version__ = "0.2.0"

# 最先执行: 清洗代理环境变量, 避免 httpx / py_clob_client 在模块级构造客户端时崩溃
# (详见 pmq/netclean.py 的故障说明)。必须位于本文件任何其他 import 之前。
from . import netclean  # noqa: E402,F401  (import 即副作用, 顺序敏感)
del netclean
