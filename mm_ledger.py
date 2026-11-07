"""
mm_ledger.py — 做市账本（纸面地基，阶段 2 前置）

依据：《Polymarket 做市交 Poly 执行方案》
- §3 阶段 1 验收：统一账本事件契约、绩效按 REALIZED 记（B19）；
- §6.3 熔断全部由账本持久化推导、账实不符即全停；
- §7 闸门 A：纸面与实盘同构、markout(5m) 中位数为过关指标；
- §10.4-3：markout/成交方向以链上 OrderFilled 为唯一真相（字段预留）。


paper 与 live 使用两个物理 SQLite 文件（mm_ledger_paper.sqlite3 /
mm_ledger_live.sqlite3），且每张表都带 mode 列、每个 MmLedger 实例
绑定单一 mode 并在写入时强校验。理由：
1. 物理隔离使 paper 的模拟成交在任何代码路径下都不可能污染 live 的
PnL/熔断推导（旧系统 B19 的教训正是纸面/实盘口径混杂）；
2. 分文件可独立备份、独立收紧权限（live 文件后续只许交易进程写）；
3. mode 列是第二道防线：文件被误打开时，查询/写入仍按 mode 过滤与校验。


- USDC 金额：整数 micro-USDC（1 USDC = 1_000_000）；
- 价格：整数 micro（price × 1_000_000，价格 ∈ [0,1]）；
- 份额：整数 micro-shares（1 股 = 1_000_000）；
- 成交额由整数乘法推导：amount = price_micro × shares_micro // 1_000_000
（向下取整，dust < 1 micro-USDC 的归属口径在此固定，不许各处自拟）；
- 对外换算一律走本模块的 *_to_micro / micro_to_decimal（Decimal），
float 只允许出现在展示层，绝不写入金额列。


orders.exchange_order_id（交易所回执 ID）、fills.tx_hash 与
fills.orderfilled_ref（链上 OrderFilled 事件引用，如 tx:log_index）——
阶段 2 对账层以回执 + 链上事件为唯一真相逐笔比对，本骨架先保证
写入面不缺字段。
"""

from __future__ import annotations

import os
import sqlite3
import time
from decimal import Decimal
from typing import Dict, List, Optional

__all__ = [
"MICRO",
"usd_to_micro",
"price_to_micro",
"shares_to_micro",
"micro_to_decimal",
"compute_markout_micro",
"MmLedger",
]

MICRO = 1_000_000 # 整数最小单位比例：金额/价格/份额统一 1e-6


def _to_micro(value) -> int:
    """Decimal/str/int → 整数 micro。拒绝 float 入参（防二进制误差进账本）。"""
    if isinstance(value, float):
        raise TypeError("金额换算禁止 float 入参，请用 Decimal 或 str")
    return int((Decimal(value) * MICRO).to_integral_value(rounding="ROUND_DOWN"))


def usd_to_micro(value) -> int:
    return _to_micro(value)


def price_to_micro(value) -> int:
    return _to_micro(value)


def shares_to_micro(value) -> int:
    return _to_micro(value)


def micro_to_decimal(micro_value: int) -> Decimal:
    return Decimal(micro_value) / Decimal(MICRO)


def compute_markout_micro(side: str, mid_at_fill_micro: int,
                          mid_at_horizon_micro: int) -> int:
    """maker 视角 markout（整数 micro 价格单位）。

    约定：我方买入成交（side=BUY）后中间价下跌为不利 → markout 为负；
    我方卖出成交后中间价上涨为不利 → markout 为负。即：
    BUY: mid_h − mid_0；SELL: −(mid_h − mid_0)。正值 = 成交后价格朝有利方向。
    """
    drift = mid_at_horizon_micro - mid_at_fill_micro
    return drift if side == "BUY" else -drift


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
id INTEGER PRIMARY KEY AUTOINCREMENT,
mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
client_order_id TEXT NOT NULL UNIQUE,
exchange_order_id TEXT,
market_id TEXT NOT NULL,
condition_id TEXT,
token_id TEXT NOT NULL,
side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
price_micro INTEGER NOT NULL,
shares_micro INTEGER NOT NULL,
post_only INTEGER NOT NULL DEFAULT 1,
status TEXT NOT NULL DEFAULT 'open',
created_at_ms INTEGER NOT NULL,
updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
id INTEGER PRIMARY KEY AUTOINCREMENT,
mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
order_id INTEGER NOT NULL REFERENCES orders(id),
exchange_order_id TEXT,
market_id TEXT NOT NULL,
token_id TEXT NOT NULL,
side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
price_micro INTEGER NOT NULL,
shares_micro INTEGER NOT NULL,
amount_micro INTEGER NOT NULL,
fee_micro INTEGER NOT NULL DEFAULT 0,
tx_hash TEXT,
orderfilled_ref TEXT,
filled_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_ledger (
id INTEGER PRIMARY KEY AUTOINCREMENT,
mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
kind TEXT NOT NULL,
amount_micro INTEGER NOT NULL,
balance_after_micro INTEGER NOT NULL,
ref TEXT,
note TEXT,
created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS markout_samples (
id INTEGER PRIMARY KEY AUTOINCREMENT,
mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
fill_id INTEGER NOT NULL REFERENCES fills(id),
market_id TEXT NOT NULL,
token_id TEXT NOT NULL,
horizon_sec INTEGER NOT NULL,
mid_at_fill_micro INTEGER NOT NULL,
mid_at_horizon_micro INTEGER NOT NULL,
markout_micro INTEGER NOT NULL,
source TEXT NOT NULL,
sampled_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_id);
CREATE INDEX IF NOT EXISTS idx_markout_market ON markout_samples(market_id, horizon_sec);
CREATE INDEX IF NOT EXISTS idx_orders_mode_status ON orders(mode, status);
CREATE INDEX IF NOT EXISTS idx_orders_mode_client ON orders(mode, client_order_id);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class MmLedger:
    """单 mode 账本。mode 绑死在实例上，写入强校验、查询强制按 mode 过滤。"""

    def __init__(self, mode: str, base_dir: str):
        if mode not in ("paper", "live"):
            raise ValueError(f"mode 必须是 paper/live，收到 {mode!r}")
        self.mode = mode
        self.path = os.path.join(base_dir, f"mm_ledger_{mode}.sqlite3")
        os.makedirs(base_dir, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- 内部校验 ------------------------------------------------------------

    def _check_int(self, name: str, value) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} 必须是整数最小单位（int），收到 {type(value).__name__}")
        return value

    def _check_mode(self, row_mode: Optional[str]) -> None:
        if row_mode is not None and row_mode != self.mode:
            raise ValueError(
                f"账本隔离校验失败：本实例 mode={self.mode}，记录 mode={row_mode}")

    # -- 订单 ---------------------------------------------------------------

    def record_order(self, *, client_order_id: str, market_id: str, token_id: str,
                     side: str, price_micro: int, shares_micro: int,
                     condition_id: Optional[str] = None,
                     exchange_order_id: Optional[str] = None,
                     post_only: bool = True, status: str = "open",
                     mode: Optional[str] = None) -> int:
        self._check_mode(mode)
        self._check_int("price_micro", price_micro)
        self._check_int("shares_micro", shares_micro)
        if status not in self.ORDER_STATUSES:
            raise ValueError(f"非法订单状态：{status!r}")
        now = _now_ms()
        cur = self._conn.execute(
            """INSERT INTO orders (mode, client_order_id, exchange_order_id,
                   market_id, condition_id, token_id, side, price_micro,
                   shares_micro, post_only, status, created_at_ms, updated_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.mode, client_order_id, exchange_order_id, market_id,
             condition_id, token_id, side, price_micro, shares_micro,
             1 if post_only else 0, status, now, now))
        self._conn.commit()
        return int(cur.lastrowid)

    # 订单状态白名单（加固 L1-08）：账本层拒绝任意字符串落库
    ORDER_STATUSES = ("open", "partially_filled", "filled", "cancelled",
                      "partially_filled_cancelled")

    def update_order_status(self, order_id: int, status: str,
                            exchange_order_id: Optional[str] = None) -> None:
        if status not in self.ORDER_STATUSES:
            raise ValueError(f"非法订单状态：{status!r}")
        self._conn.execute(
            """UPDATE orders SET status=?, updated_at_ms=?,
                   exchange_order_id=COALESCE(?, exchange_order_id)
               WHERE id=? AND mode=?""",
            (status, _now_ms(), exchange_order_id, order_id, self.mode))
        self._conn.commit()

    def get_orders(self, market_id: Optional[str] = None) -> List[Dict]:
        sql = "SELECT * FROM orders WHERE mode=?"
        args: List = [self.mode]
        if market_id:
            sql += " AND market_id=?"
            args.append(market_id)
        return [dict(r) for r in self._conn.execute(sql, args)]

    def get_open_orders(self) -> List[Dict]:
        """仅未终结订单（open / partially_filled）——启动恢复专用。

        旧路径用 get_orders() 全表物化后在 Python 里筛 status：账本达
        百万行量级时每次重启白扫全表（实测 ~11.5s/百万行且行 dict 全量
        进内存）。本查询走 idx_orders_mode_status，只回未终结行。
        """
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM orders WHERE mode=? AND status IN ('open','partially_filled')",
            (self.mode,))]

    def max_client_order_seq(self, prefix: str = "paper-") -> int:
        """client_order_id 尾随数字段的最大值（引擎订单序号续接用）。

        语义与旧实现等价：旧实现遍历全表、对 startswith(prefix) 的行取
        最后一个 '-' 之后的 int（不可解析则跳过）。本实现在 SQL 内完成：
        尾随数字段起点 = length(rtrim(id, 数字集)) + 1，CAST 取值后取
        MAX，全程走 (mode, client_order_id) 覆盖索引、不物化行。
        无匹配行返回 0。
        守卫条件「尾随数字段之前必须是 '-'」保证与旧口径严格等价：
        形如 paper-abc123 的 id 旧实现 int('abc123') 失败跳过，新实现
        同样不计。等价性已由 test_line2_resilience 在真实账本副本
        （6.5 万行）与合成边界 id 上断言。
        """
        row = self._conn.execute(
            """SELECT MAX(CAST(substr(client_order_id,
                       length(rtrim(client_order_id, '0123456789')) + 1)
                      AS INTEGER)) AS max_seq
               FROM orders WHERE mode=? AND client_order_id LIKE ?
                 AND substr(client_order_id,
                            length(rtrim(client_order_id, '0123456789')), 1) = '-'""",
            (self.mode, prefix + "%")).fetchone()
        return int(row["max_seq"]) if row and row["max_seq"] is not None else 0

    # -- 成交 ---------------------------------------------------------------

    def record_fill(self, *, order_id: int, market_id: str, token_id: str,
                    side: str, price_micro: int, shares_micro: int,
                    fee_micro: int = 0,
                    exchange_order_id: Optional[str] = None,
                    tx_hash: Optional[str] = None,
                    orderfilled_ref: Optional[str] = None,
                    mode: Optional[str] = None) -> int:
        self._check_mode(mode)
        self._check_int("price_micro", price_micro)
        self._check_int("shares_micro", shares_micro)
        self._check_int("fee_micro", fee_micro)
        # 累计成交量与订单状态校验（加固 L1-08）：旧实现不查订单剩余量，
        # 20 股订单可写入 40 股成交。账本层兜底，引擎 bug 不许污染账本。
        orow = self._conn.execute(
            "SELECT shares_micro, status FROM orders WHERE id=? AND mode=?",
            (order_id, self.mode)).fetchone()
        if orow is None:
            raise ValueError(f"record_fill：订单不存在 id={order_id}")
        if orow["status"] not in ("open", "partially_filled"):
            raise ValueError(
                f"record_fill：订单状态 {orow['status']!r} 不可再成交 id={order_id}")
        filled_so_far = self._conn.execute(
            "SELECT COALESCE(SUM(shares_micro),0) AS s FROM fills "
            "WHERE order_id=? AND mode=?",
            (order_id, self.mode)).fetchone()["s"]
        if filled_so_far + shares_micro > orow["shares_micro"]:
            raise ValueError(
                f"record_fill：累计成交 {filled_so_far + shares_micro} 超过 "
                f"订单量 {orow['shares_micro']} id={order_id}")
        amount_micro = price_micro * shares_micro // MICRO  # 整数乘法推导，见模块头口径
        cur = self._conn.execute(
            """INSERT INTO fills (mode, order_id, exchange_order_id, market_id,
                   token_id, side, price_micro, shares_micro, amount_micro,
                   fee_micro, tx_hash, orderfilled_ref, filled_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.mode, order_id, exchange_order_id, market_id, token_id, side,
             price_micro, shares_micro, amount_micro, fee_micro, tx_hash,
             orderfilled_ref, _now_ms()))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_fills(self, market_id: Optional[str] = None) -> List[Dict]:
        sql = "SELECT * FROM fills WHERE mode=?"
        args: List = [self.mode]
        if market_id:
            sql += " AND market_id=?"
            args.append(market_id)
        return [dict(r) for r in self._conn.execute(sql, args)]

    # -- 资金流水 -------------------------------------------------------------

    def record_cash(self, *, kind: str, amount_micro: int,
                    ref: Optional[str] = None, note: Optional[str] = None,
                    mode: Optional[str] = None) -> int:
        self._check_mode(mode)
        self._check_int("amount_micro", amount_micro)
        balance_after = self.get_cash_balance_micro() + amount_micro
        cur = self._conn.execute(
            """INSERT INTO cash_ledger (mode, kind, amount_micro,
                   balance_after_micro, ref, note, created_at_ms)
               VALUES (?,?,?,?,?,?,?)""",
            (self.mode, kind, amount_micro, balance_after, ref, note, _now_ms()))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_cash_balance_micro(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_micro),0) AS bal FROM cash_ledger WHERE mode=?",
            (self.mode,)).fetchone()
        return int(row["bal"])

    def get_cash_entries(self) -> List[Dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM cash_ledger WHERE mode=? ORDER BY id", (self.mode,))]

    # -- markout 采样 -----------------------------------------------------------

    def record_markout(self, *, fill_id: int, market_id: str, token_id: str,
                       side: str, mid_at_fill_micro: int,
                       mid_at_horizon_micro: int, horizon_sec: int = 300,
                       source: str = "paper_sim",
                       mode: Optional[str] = None) -> int:
        self._check_mode(mode)
        self._check_int("mid_at_fill_micro", mid_at_fill_micro)
        self._check_int("mid_at_horizon_micro", mid_at_horizon_micro)
        # 幂等（加固 L1-07）：同一 (fill, horizon) 已有样本则回既有 id，
        # 不重复写入污染闸门中位数（schema UNIQUE 迁移留待闸门结束后）。
        existing = self._conn.execute(
            "SELECT id FROM markout_samples WHERE mode=? AND fill_id=? "
            "AND horizon_sec=?",
            (self.mode, fill_id, horizon_sec)).fetchone()
        if existing is not None:
            return int(existing["id"])
        markout = compute_markout_micro(side, mid_at_fill_micro, mid_at_horizon_micro)
        cur = self._conn.execute(
            """INSERT INTO markout_samples (mode, fill_id, market_id, token_id,
                   horizon_sec, mid_at_fill_micro, mid_at_horizon_micro,
                   markout_micro, source, sampled_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (self.mode, fill_id, market_id, token_id, horizon_sec,
             mid_at_fill_micro, mid_at_horizon_micro, markout, source, _now_ms()))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_markout_samples(self, market_id: Optional[str] = None,
                            horizon_sec: int = 300) -> List[Dict]:
        sql = "SELECT * FROM markout_samples WHERE mode=? AND horizon_sec=?"
        args: List = [self.mode, horizon_sec]
        if market_id:
            sql += " AND market_id=?"
            args.append(market_id)
        return [dict(r) for r in self._conn.execute(sql, args)]

    def median_markout_micro(self, market_id: str, horizon_sec: int = 300) -> Optional[int]:
        """markout 中位数（整数 micro）——闸门 A / §5.1-5 / §6.3 的共同判定量。"""
        rows = self._conn.execute(
            """SELECT markout_micro FROM markout_samples
               WHERE mode=? AND market_id=? AND horizon_sec=?
               ORDER BY markout_micro""",
            (self.mode, market_id, horizon_sec)).fetchall()
        if not rows:
            return None
        values = [r["markout_micro"] for r in rows]
        n = len(values)
        mid = n // 2
        if n % 2 == 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) // 2
