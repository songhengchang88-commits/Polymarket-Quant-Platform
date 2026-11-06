"""
risk_breaker.py — 做市熔断状态机（方案 §6.3 的代码化，阶段 2 前置）

依据：《Polymarket 做市交 Poly 执行方案》
- §6.3 熔断表（程序强制执行、重启不失效、全部由账本持久化推导）：
  * 单日亏损 −2% 总资金 → 当日停市、全撤、次日复盘后才恢复；
  * 总回撤 −10% 仓位减半；−20% 停 7 天全面复盘；−30% 项目终止评估；
  * 账实不符（任一例对账差异）→ 立即全停，先对账后谈恢复。
- §12.1-3 与 §12.3：总回撤 −30% = 终止线，触发即停机结项、不自动重启；
- §12.2：日亏熔断 −2%、回撤等阈值均按**当前净值**比例计算（净值缩放）——
  $200 种子期日亏限额 −$4 由 2% × $200 自动得出，本模块不写死任何美元数。

设计口径（与方案的对应/补足，均在此写明，不另行自拟阈值）：
- 金额一律整数 micro-USDC（与 mm_ledger 同口径 1e-6），拒绝 float 入参；
- 交易日按 Asia/Shanghai（UTC+8 固定偏移）切分，与运营时区一致；
- −10% 回撤「仓位减半」不是停机态：五态之外以 decision.position_scale=0.5
  建议值输出，状态仍为 NORMAL；
- COOLDOWN_7D 冷却期满后把净值高点基线重置为当前净值（对应 §6.3「全面
  复盘」后以复盘结论重新起算），否则同一段回撤会无限重复触发；重置动作
  写入审计留痕；
- RECONCILE_HALT 与 TERMINATED 均不自动恢复：前者只能经
  clear_reconcile_halt() 人工清除（先对账后谈恢复），后者只能经
  manual_reset_terminated() 人工复位，两者都必须给出 operator 与 note
  并写入持久化审计日志；
- 状态以 JSON 落盘（与账本同目录），写文件用临时文件 + 原子替换，
  进程重启后状态不丢失（§6.3「重启不失效」）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

__all__ = [
    "NORMAL", "DAILY_HALT", "COOLDOWN_7D", "TERMINATED", "RECONCILE_HALT",
    "DAILY_LOSS_PCT", "DD_HALVE_PCT", "DD_COOLDOWN_PCT", "DD_TERMINATE_PCT",
    "COOLDOWN_DAYS",
    "BreakerSnapshot", "BreakerDecision", "RiskBreaker",
    "snapshot_from_ledger",
]

# -- 阈值（方案 §6.3 / §12.2，均为比例，不写死金额） -------------------------
DAILY_LOSS_PCT = 2        # 单日亏损 −2% 当前净值 → DAILY_HALT
DD_HALVE_PCT = 10         # 总回撤 −10% → 仓位减半（建议值，非停机态）
DD_COOLDOWN_PCT = 20      # 总回撤 −20% → COOLDOWN_7D
DD_TERMINATE_PCT = 30     # 总回撤 −30% → TERMINATED（终止线）
COOLDOWN_DAYS = 7         # §6.3：−20% 停 7 天

# -- 状态 ---------------------------------------------------------------------
NORMAL = "NORMAL"
DAILY_HALT = "DAILY_HALT"
COOLDOWN_7D = "COOLDOWN_7D"
TERMINATED = "TERMINATED"
RECONCILE_HALT = "RECONCILE_HALT"

_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 固定偏移（无 tzdata 依赖）


def _day_key(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=_TZ).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _check_micro(name: str, value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} 必须是整数 micro（int），收到 {type(value).__name__}")
    return value


@dataclass
class BreakerSnapshot:
    """一次评估的输入（由账本查询组装，金额整数 micro）。

    equity_now_micro：当前净值（现金 + 持仓未实现盈亏后的总权益）；
    equity_peak_micro：净值历史高点（回撤基准）；
    day_start_equity_micro：当日日初净值（当日盈亏 = 今值 − 日初值，
    含已实现与未实现，与任务口径一致）；
    reconcile_ok：对账状态标志，False = 存在任一例账实不符。
    """
    equity_now_micro: int
    equity_peak_micro: int
    day_start_equity_micro: int
    reconcile_ok: bool = True

    def __post_init__(self) -> None:
        _check_micro("equity_now_micro", self.equity_now_micro)
        _check_micro("equity_peak_micro", self.equity_peak_micro)
        _check_micro("day_start_equity_micro", self.day_start_equity_micro)

    @property
    def daily_pnl_micro(self) -> int:
        return self.equity_now_micro - self.day_start_equity_micro

    @property
    def drawdown_micro(self) -> int:
        """相对历史高点的回撤额（正数 = 处于回撤）。"""
        return self.equity_peak_micro - self.equity_now_micro


@dataclass
class BreakerDecision:
    state: str
    can_open: bool          # 是否允许开新仓
    reduce_only: bool       # 是否只许平仓/撤单（停机态均为 True）
    position_scale: float   # 仓位缩放建议（1.0 正常；0.5 = 回撤减半）
    reason: str
    state_since_ms: Optional[int] = None
    cooldown_until_ms: Optional[int] = None


@dataclass
class _Persisted:
    state: str = NORMAL
    state_since_ms: int = 0
    halt_day: Optional[str] = None          # DAILY_HALT 触发的交易日
    cooldown_until_ms: Optional[int] = None
    peak_baseline_micro: Optional[int] = None  # 冷却复盘后重置的高点基线
    audit: List[dict] = field(default_factory=list)


class RiskBreaker:
    """熔断状态机。状态落盘 state_dir/risk_breaker_state.json。"""

    FILE_NAME = "risk_breaker_state.json"

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, self.FILE_NAME)
        self._s = self._load()

    # -- 持久化 ---------------------------------------------------------------

    def _load(self) -> _Persisted:
        if not os.path.exists(self.path):
            return _Persisted()
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _Persisted(
            state=raw.get("state", NORMAL),
            state_since_ms=raw.get("state_since_ms", 0),
            halt_day=raw.get("halt_day"),
            cooldown_until_ms=raw.get("cooldown_until_ms"),
            peak_baseline_micro=raw.get("peak_baseline_micro"),
            audit=raw.get("audit", []),
        )

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "state": self._s.state,
                "state_since_ms": self._s.state_since_ms,
                "halt_day": self._s.halt_day,
                "cooldown_until_ms": self._s.cooldown_until_ms,
                "peak_baseline_micro": self._s.peak_baseline_micro,
                "audit": self._s.audit,
            }, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)  # 原子替换，防写一半进程崩溃

    def _audit(self, now_ms: int, event: str, detail: str) -> None:
        self._s.audit.append({"at_ms": now_ms, "event": event, "detail": detail})

    def _enter(self, now_ms: int, state: str, detail: str) -> None:
        if self._s.state != state:
            self._audit(now_ms, f"{self._s.state}->{state}", detail)
            self._s.state = state
            self._s.state_since_ms = now_ms

    # -- 评估 -------------------------------------------------------------------

    def _effective_peak(self, snap: BreakerSnapshot) -> int:
        """回撤基准高点：未重置基线时用快照的历史高点；冷却复盘/人工复位
        重置过基线后，以基线为起点、只认基线之后的新高（否则同一段历史
        回撤会被反复计入，见模块头口径）。"""
        if self._s.peak_baseline_micro is None:
            return snap.equity_peak_micro
        return max(self._s.peak_baseline_micro, snap.equity_now_micro)

    def evaluate(self, snap: BreakerSnapshot,
                 now_ms: Optional[int] = None) -> BreakerDecision:
        now = _now_ms() if now_ms is None else now_ms
        s = self._s
        today = _day_key(now)

        peak = self._effective_peak(snap)
        dd = peak - snap.equity_now_micro
        equity = snap.equity_now_micro

        # 1) TERMINATED 终局：任何输入都不自动恢复（§12.1-3）
        if s.state == TERMINATED:
            return self._decision(snap, now, "终止线已触发，项目停机结项；"
                                             "恢复只能由人工复位（manual_reset_terminated）")

        # 2) 账实不符：任一例即全停（§6.3），且在任何其他判定之前
        if not snap.reconcile_ok:
            self._enter(now, RECONCILE_HALT, "对账差异：账实不符，立即全停")
            s.halt_day = None
            self._save()
            return self._decision(snap, now, "账实不符，立即全停，先对账后谈恢复")
        if s.state == RECONCILE_HALT:
            # 对账转 OK 也不自动恢复，必须人工清除（留痕）
            return self._decision(snap, now, "账实不符熔断未清除：须先完成对账，"
                                             "再经 clear_reconcile_halt 人工清除")

        # 3) 终止线：回撤 ≥30% 当前净值口径（峰值基线相对今值）
        if equity > 0 and dd * 100 >= DD_TERMINATE_PCT * peak and peak > 0:
            self._enter(now, TERMINATED,
                        f"总回撤 {dd} micro 达峰值 {peak} 的 {DD_TERMINATE_PCT}% 终止线")
            self._save()
            return self._decision(snap, now, "触发 −30% 终止线：停机结项，不自动重启")

        # 4) 冷却：已在冷却中未到期 → 保持；到期 → 复盘重置基线后回 NORMAL
        if s.state == COOLDOWN_7D:
            if s.cooldown_until_ms is not None and now < s.cooldown_until_ms:
                return self._decision(snap, now, "−20% 回撤冷却中（停 7 天全面复盘）")
            # 期满：以当前净值重置高点基线，避免同一回撤无限重复触发
            s.peak_baseline_micro = snap.equity_now_micro
            s.cooldown_until_ms = None
            self._enter(now, NORMAL, "冷却期满，复盘后以当前净值重置高点基线")
            self._save()
            return self._decision(snap, now, "冷却期满恢复，基线已重置")

        # 5) 回撤 ≥20% → 进冷却 7 天
        if peak > 0 and dd * 100 >= DD_COOLDOWN_PCT * peak:
            s.cooldown_until_ms = now + COOLDOWN_DAYS * 24 * 3600 * 1000
            self._enter(now, COOLDOWN_7D,
                        f"总回撤 {dd} micro 达峰值 {peak} 的 {DD_COOLDOWN_PCT}%")
            self._save()
            return self._decision(snap, now, "总回撤达 −20%：停 7 天全面复盘")

        # 6) 当日熔断跨日恢复（§6.3：次日复盘后才恢复——以跨日为恢复点，
        #    复盘动作由运营侧在恢复前完成，本机只按日界放行并留痕）
        if s.state == DAILY_HALT:
            if s.halt_day == today:
                return self._decision(snap, now, "当日亏损熔断中，次日复盘后恢复")
            self._enter(now, NORMAL, f"跨日恢复（熔断日 {s.halt_day} → {today}）")
            s.halt_day = None
            self._save()
            return self._decision(snap, now, "跨日恢复")

        # 7) 单日亏损 ≥2% 当前净值 → 当日停市（$200 时即 −$4，比例自动得出）
        daily_limit = equity * DAILY_LOSS_PCT // 100 if equity > 0 else 0
        if equity > 0 and -snap.daily_pnl_micro >= daily_limit and snap.daily_pnl_micro < 0:
            s.halt_day = today
            self._enter(now, DAILY_HALT,
                        f"当日盈亏 {snap.daily_pnl_micro} micro 达限额 −{daily_limit} micro"
                        f"（当前净值 2%）")
            self._save()
            return self._decision(snap, now, "单日亏损达 −2% 当前净值：当日停市、全撤")

        # 8) 正常：回撤 ≥10% 给仓位减半建议（非停机）；基线后创新高则推进基线
        if s.peak_baseline_micro is not None and equity > s.peak_baseline_micro:
            s.peak_baseline_micro = equity
            self._save()
        return self._decision(snap, now, "正常")

    def _decision(self, snap: BreakerSnapshot, now_ms: int,
                  reason: str) -> BreakerDecision:
        s = self._s
        halted = s.state in (DAILY_HALT, COOLDOWN_7D, TERMINATED, RECONCILE_HALT)
        scale = 1.0
        if not halted:
            peak = self._effective_peak(snap)
            if peak > 0 and (peak - snap.equity_now_micro) * 100 >= DD_HALVE_PCT * peak:
                scale = 0.5  # §6.3：总回撤 −10% 仓位减半
        return BreakerDecision(
            state=s.state,
            can_open=not halted,
            reduce_only=halted,
            position_scale=scale,
            reason=reason,
            state_since_ms=s.state_since_ms or None,
            cooldown_until_ms=s.cooldown_until_ms,
        )

    # -- 人工复位（均须 operator + note 并留痕） ----------------------------------

    def clear_reconcile_halt(self, *, operator: str, note: str,
                             now_ms: Optional[int] = None) -> None:
        """账实不符熔断的人工清除：对账完成后调用（§6.3 先对账后谈恢复）。"""
        if not operator or not note:
            raise ValueError("人工清除必须给出 operator 与 note（留痕要求）")
        if self._s.state != RECONCILE_HALT:
            raise ValueError(f"当前状态 {self._s.state} 不是 RECONCILE_HALT，无可清除")
        now = _now_ms() if now_ms is None else now_ms
        self._s.audit.append({"at_ms": now, "event": "RECONCILE_HALT->NORMAL(manual)",
                              "detail": f"operator={operator}; note={note}"})
        self._s.state = NORMAL
        self._s.state_since_ms = now
        self._save()

    def manual_reset_terminated(self, *, operator: str, note: str,
                                now_ms: Optional[int] = None) -> None:
        """终止线的人工复位（§12.1-3：是否复活只能由 Jason 另行决定——本函数
        只提供带留痕的复位通道，不判断授权，调用方须已获 Jason 批准）。"""
        if not operator or not note:
            raise ValueError("人工复位必须给出 operator 与 note（留痕要求）")
        if self._s.state != TERMINATED:
            raise ValueError(f"当前状态 {self._s.state} 不是 TERMINATED，无可复位")
        now = _now_ms() if now_ms is None else now_ms
        self._s.audit.append({"at_ms": now, "event": "TERMINATED->NORMAL(manual)",
                              "detail": f"operator={operator}; note={note}"})
        self._s.state = NORMAL
        self._s.state_since_ms = now
        self._s.peak_baseline_micro = None  # 复位后高点由后续快照重新起算
        self._save()

    @property
    def state(self) -> str:
        return self._s.state

    def audit_log(self) -> List[dict]:
        return list(self._s.audit)


def snapshot_from_ledger(ledger, *, unrealized_pnl_micro: int = 0,
                         equity_peak_micro: int,
                         day_start_equity_micro: int,
                         reconcile_ok: bool = True) -> BreakerSnapshot:
    """由账本组装快照：当前净值 = 账本现金余额 + 未实现盈亏（持仓盯市由
    调用方给出）；峰值/日初净值由调用方的净值序列（账本持久化推导）给出。"""
    _check_micro("unrealized_pnl_micro", unrealized_pnl_micro)
    equity_now = ledger.get_cash_balance_micro() + unrealized_pnl_micro
    return BreakerSnapshot(
        equity_now_micro=equity_now,
        equity_peak_micro=equity_peak_micro,
        day_start_equity_micro=day_start_equity_micro,
        reconcile_ok=reconcile_ok,
    )
