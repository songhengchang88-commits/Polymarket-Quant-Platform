"""
paper_engine.py — 纸面同构做市引擎核心（阶段 2 第一步：纸面引擎）

定位：闸门 A（方案 §7）的纸面同构引擎核心。只用公开行情快照 +
模拟撮合（CostModel）驱动，不下单、不用凭证、不连真实账户；
账本强制 paper 模式（MmLedger(mode="paper")），与 live 物理隔离。

依据：《Polymarket 做市交 Poly 执行方案》
- §4 打法 1：双边做市主线（post-only 双边挂单 + 库存偏斜）；
- §5.2 分数段：≥80 main 标准份额／65–79 watch 半份／50–64 paper
  纸面跟踪／<50 drop——纸面期入选（可报价）阈值 = 50（与
  market_scoring 的入选口径一致），份额系数见下方常量登记；
- §6.2 仓位纪律：单市场敞口 ≤ 净值 10%、总库存 ≤ 60%、常留 ≥40%
  现金；单笔 = 目标份额最小档（奖励门槛 20 股起步）；库存偏斜
  50% 后撤一档、100% 停该方向；到期 48h 停开仓／24h 降至 30%／
  6h 原则清零；
- §6.3 熔断：日亏 −2% 程序强制（本模块实现当日净值口径的最小版，
  回撤分层与连续 3 日 markout 拉黑等组合层熔断未实现，见交付说明）；
- §7 闸门 A：纸面与实盘同 CostModel——本模块的模拟撮合即纸面侧
  CostModel 核心，markout(5m) 采样写入 mm_ledger 供闸门判定；
- §11 交易 bot 五态状态机：QUIET / TRENDING / EVENT / REDUCE_ONLY /
  HALTED（借 poly-maker 状态机设计，§10.2）。

数值来源分两类，逐项在常量区注明：
- 【方案】：方案原文给出的数字与规则；
- 【自拟】：方案未给数值的实现参数，集中登记于本文件常量区与
  交付说明《纸面引擎交付说明.md》的【自拟】参数总表，
  须以纸面实测校准，不许当作方案原文引用。

金额口径：一切金额/价格/份额经 mm_ledger 的整数 micro 换算
（float 入参会被账本拒绝；本模块内部换算一律走 Decimal(str)）。
存量敞口按当前中间价 MtM 计，增量（新报价/新成交）按其金额计，
两口径在 _check_buy 处统一，属保守口径【自拟】（见交付说明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from market_scoring import SCORE_BANDS
from mm_ledger import MICRO, MmLedger, price_to_micro, shares_to_micro, usd_to_micro

__all__ = [
    "PaperEngine",
    "STATE_QUIET", "STATE_TRENDING", "STATE_EVENT",
    "STATE_REDUCE_ONLY", "STATE_HALTED",
]

# ---------------------------------------------------------------------------
# 状态机状态名（§11）
# ---------------------------------------------------------------------------

STATE_QUIET = "QUIET"                # 正常双边报价
STATE_TRENDING = "TRENDING"          # 波动放大：价差打宽、份额缩量
STATE_EVENT = "EVENT"                # 事件窗口/到期停开仓：只许减仓方向
STATE_REDUCE_ONLY = "REDUCE_ONLY"    # 只减仓：只挂卖单
STATE_HALTED = "HALTED"              # 全停：全撤、不报价

# ---------------------------------------------------------------------------
# 【方案】常量（出处：方案 §5.2 / §6.2 / §6.3 / §7 / §12）
# ---------------------------------------------------------------------------

MAX_MARKET_EXPOSURE_PCT = 10.0    # §6.2：单市场库存敞口 ≤ 净值 10%
MAX_TOTAL_EXPOSURE_PCT = 60.0     # §6.2：总库存敞口 ≤ 净值 60%（现金 ≥40%）
QUOTE_MIN_SCORE = 50.0            # §5.2：入选纸面跟踪阈值（main 80 / watch 65）
DEFAULT_SEED_USD = "200"          # §12.1：种子金上限 $200（纸面初始资金默认）
DEFAULT_TARGET_SHARES = "20"      # §6.2：单笔 = 目标份额最小档，奖励门槛 20 股起步
SKEW_WIDEN_PCT = 50.0             # §6.2：单腿净库存达上限 50% → 该方向后撤一档
SKEW_STOP_PCT = 100.0             # §6.2：达 100% → 停该方向报价
EXPIRY_STOP_OPEN_H = 48.0         # §5.1-4/§6.2：到期前 48h 停止新开仓
EXPIRY_REDUCE_H = 24.0            # §6.2：到期前 24h 存量降至上限 30% 以内
EXPIRY_CLEAR_H = 6.0              # §6.2：到期前 6h 原则清零
EXPIRY_REDUCE_CAP_PCT = 30.0      # §6.2：24h 窗口的库存上限比例（对卖单目标口径）
DAILY_LOSS_HALT_PCT = 2.0         # §6.3：单日亏损 −2% → 当日停市（最小实现）
MARKOUT_HORIZON_SEC = 300         # §2/§7：markout 采样窗口 5 分钟

# 分数段份额系数【方案 §5.2 动作 + 自拟数值】：
# main 标准份额 = 1.0、watch 半份 = 0.5 为方案原文；paper 段在实盘
# 语境是「只纸面跟踪」，纸面期为采集样本以半份报价【自拟】。
BAND_SIZE_FACTOR = {"main": 1.0, "watch": 0.5, "paper": 0.5}

# ---------------------------------------------------------------------------
# 【自拟】常量（方案未给数值，待纸面校准；交付说明有总表）
# ---------------------------------------------------------------------------

SELF_HALF_SPREAD_PCT = 1.0        # 报价半价差 = 中间价 × 1%（总价差约 2%，§5.2 甜区 1–2.5% 内取中）
SELF_QUEUE_HAIRCUT = 0.5          # 排队折损：穿越量按 50% 计为可成交量（排在我方前面的队列吃掉一半）
SELF_TRENDING_WIDEN_MULT = 2.0    # TRENDING 时半价差倍数（§11「加宽」的数值化）
SELF_TRENDING_SIZE_MULT = 0.5     # TRENDING 时份额倍数（打宽/缩量）
SELF_VOL_WINDOW_SEC = 60.0        # 波动观测窗口（秒）
SELF_VOL_TRENDING_PCT = 2.0       # 窗口内中间价相对振幅 ≥2% → TRENDING
SELF_DEFAULT_TICK = "0.01"        # 盘口与市场都缺 tick 时的兜底（登记 missing，不许静默）
SELF_FEE_BPS = 0                  # 手续费基点：默认 0——方案 §1「做市方多数市场零交易费」；
                                  # 现行官方逐市场费率表未核实【未证实】，以参数注入预留


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _p2m(price) -> int:
    """float/str 价格 → 整数 micro（走 Decimal(str)，不让 float 直入账本换算）。"""
    return price_to_micro(Decimal(str(price)))


def _s2m(shares) -> int:
    return shares_to_micro(Decimal(str(shares)))


def _mid_micro(book: Dict) -> Optional[int]:
    """Book dict → 中间价 micro。空盘（无双边）返回 None。"""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    return (_p2m(book["best_bid"]) + _p2m(book["best_ask"])) // 2


def _parse_end_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _band_of(score: float) -> Optional[str]:
    for threshold, band, _action in SCORE_BANDS:
        if score >= threshold:
            return band
    return None


@dataclass
class _RestingOrder:
    """纸面在挂订单（引擎内存态；账本 orders 表同步留痕）。"""
    client_order_id: str
    ledger_order_id: int
    market_id: str
    token_id: str
    side: str                    # BUY / SELL
    price_micro: int
    shares_micro: int
    filled_micro: int = 0
    counted_through_micro: int = 0   # 累计已计穿越量（折损后，单调不减）

    @property
    def remaining_micro(self) -> int:
        return self.shares_micro - self.filled_micro


@dataclass
class _MarketReg:
    market: Dict
    score: Optional[float]
    band: Optional[str]
    quotable: bool
    target_shares_micro: int
    end_dt: Optional[datetime]
    state: str = STATE_QUIET
    state_reason: str = ""
    reduce_only_signal: bool = False     # 信号注入：只减仓（§11.3 双源确认后的注入点）
    event_window: Optional[tuple] = None  # (start_dt, end_dt) 注入的事件窗口


# ---------------------------------------------------------------------------
# 引擎主体
# ---------------------------------------------------------------------------

class PaperEngine:
    """纸面同构做市引擎核心（步进驱动：on_book 喂快照，无线程、无网络）。

    参数：
    - ledger：MmLedger(mode="paper") 实例（非 paper 直接拒绝）；
    - seed_usd：纸面初始资金（账本无任何流水时才注入 deposit，
      默认 "200" = §12.1 种子口径，可配）；
    - base_shares：单笔目标份额（默认 "20"，§6.2 最小档）；
    - fee_bps：手续费基点【自拟/未证实】，默认 0（见常量登记）；
    - breaker：外部熔断回调 fn(engine) -> bool，每步评估，
      返回 True → HALTED（§6.3 熔断注入点）；
    - event_provider：事件日历回调 fn(market_id, now) -> bool，
      返回 True 表示该市场处于事件窗口（与 set_event_window 并行，
      任一命中即 EVENT）；真实日历数据源未接，见交付说明未实现项；
    - on_halt：HALTED 触发时的通知回调 fn(reason)（可选，仅通知）。
    """

    def __init__(self, ledger: MmLedger, *, seed_usd: str = DEFAULT_SEED_USD,
                 base_shares: str = DEFAULT_TARGET_SHARES,
                 fee_bps: int = SELF_FEE_BPS,
                 half_spread_pct: float = SELF_HALF_SPREAD_PCT,
                 queue_haircut: float = SELF_QUEUE_HAIRCUT,
                 breaker: Optional[Callable[["PaperEngine"], bool]] = None,
                 event_provider: Optional[Callable[[str, datetime], bool]] = None,
                 on_halt: Optional[Callable[[str], None]] = None):
        if ledger.mode != "paper":
            raise ValueError("纸面引擎只接受 paper 账本（live 隔离，见 mm_ledger）")
        self.ledger = ledger
        self._fee_bps = int(fee_bps)
        self._half_spread_pct = float(half_spread_pct)
        self._queue_haircut = float(queue_haircut)
        self._breaker = breaker
        self._event_provider = event_provider
        self._on_halt = on_halt
        self._base_shares_micro = _s2m(base_shares)

        # 种子金：仅当账本零流水时注入（重启复用同一账本不重复入金）
        if not ledger.get_cash_entries():
            ledger.record_cash(kind="deposit", amount_micro=usd_to_micro(seed_usd),
                               note="paper seed（§12.1 种子口径，金额可配）")

        self._markets: Dict[str, _MarketReg] = {}
        self._resting: Dict[str, _RestingOrder] = {}          # client_order_id → order
        self._positions: Dict[str, Dict[str, Dict[str, int]]] = {}  # market → token → {shares_micro, cost_micro}
        self._last_books: Dict[str, Dict[str, Dict]] = {}     # market → {token: Book}
        self._last_mid: Dict[str, int] = {}                   # token → 最近中间价 micro
        self._mid_hist: Dict[str, List[tuple]] = {}           # market → [(ts_sec, yes_mid_float)]
        self._markout_tasks: List[Dict] = []                  # 待采样任务
        self._seq = 0

        self._halted_reason: Optional[str] = None
        self._hb_stale = False        # 心跳 on_stale 挂钩：停发新单（不开新仓）
        self._hb_dead = False         # 心跳 on_dead 挂钩：闩锁全停（须 clear_halt 复位）
        self._day_anchor_date = None  # 当日净值锚（§6.3 日亏口径）
        self._day_anchor_equity: Optional[int] = None

        self.stats = {
            "quotes_placed": 0,
            "quotes_blocked_limit": 0,   # 报价路径被仓位规则拦截计数
            "fills": 0,
            "fills_rejected_limit": 0,   # 成交路径被仓位规则拦截计数
            "cancels": 0,
            "markouts_sampled": 0,
            "markouts_pending_no_book": 0,
            "halts": 0,
        }

    # -- 注册与注入 -----------------------------------------------------------

    def register_market(self, market: Dict, *, score_result: Optional[Dict] = None,
                        score: Optional[float] = None,
                        target_shares=None) -> Dict:
        """登记一个市场。评分来源二选一：market_scoring.score_market 的
        结果 dict（score_result，含 vetoed/band/score），或直接给 score 数值。
        被否决或 band=drop → quotable=False（只登记不报价）。"""
        market_id = str(market.get("id"))
        sc: Optional[float] = None
        band: Optional[str] = None
        quotable = False
        if score_result is not None:
            if not score_result.get("vetoed"):
                sc = score_result.get("score")
                band = score_result.get("band")
                quotable = band in BAND_SIZE_FACTOR and sc is not None and sc >= QUOTE_MIN_SCORE
        elif score is not None:
            sc = float(score)
            band = _band_of(sc)
            quotable = band in BAND_SIZE_FACTOR and sc >= QUOTE_MIN_SCORE
        reg = _MarketReg(
            market=market, score=sc, band=band, quotable=quotable,
            target_shares_micro=_s2m(target_shares) if target_shares is not None
            else self._base_shares_micro,
            end_dt=_parse_end_date(market.get("end_date") or ""),
        )
        self._markets[market_id] = reg
        self._positions.setdefault(market_id, {})
        return {"market_id": market_id, "score": sc, "band": band, "quotable": quotable}

    def set_event_window(self, market_id: str, start: datetime, end: datetime) -> None:
        """注入事件窗口（§11 EVENT 的日程化入口；真实日历数据源未接）。"""
        self._markets[market_id].event_window = (start, end)

    def set_reduce_only(self, market_id: str, flag: bool = True) -> None:
        """信号注入：该市场只减仓（§11.3：双源确认后才许的动作，注入即代表已确认）。"""
        self._markets[market_id].reduce_only_signal = bool(flag)

    # -- 熔断 / 心跳挂钩 -------------------------------------------------------

    def trip_halt(self, reason: str) -> None:
        if self._halted_reason is None:
            self._halted_reason = reason
            self.stats["halts"] += 1
            if self._on_halt:
                self._on_halt(reason)

    def clear_halt(self) -> None:
        """人工复位：清 HALTED 与心跳 dead 闩锁（对齐 heartbeat_loop 的
        dead 闩锁口径——恢复被接受不自动开闸，须显式复位）。"""
        self._halted_reason = None
        self._hb_dead = False

    def on_heartbeat_stale(self) -> None:
        """心跳挂钩（heartbeat_loop.HeartbeatLoop 的 on_stale 回调口径，
        零参）：心跳断供达 stale 阈值——停发新单，不开新仓（卖单减仓仍许）。"""
        self._hb_stale = True

    def on_heartbeat_dead(self) -> None:
        """心跳挂钩（on_dead 回调口径，零参）：断供达 dead 阈值——闩锁全停，
        下一步起全撤不报价，须 clear_halt() 人工复位。"""
        self._hb_dead = True
        self.trip_halt("心跳 dead（on_dead 挂钩触发）")

    def on_heartbeat_recover(self) -> None:
        """心跳挂钩（on_recover 回调口径，零参）：只清 stale；
        dead 闩锁不在此清除（见 clear_halt）。"""
        self._hb_stale = False

    # -- 估值与仓位 -------------------------------------------------------------

    def _token_mid(self, token_id: str) -> Optional[int]:
        return self._last_mid.get(token_id)

    def position_shares_micro(self, market_id: str, token_id: str) -> int:
        return self._positions.get(market_id, {}).get(token_id, {}).get("shares_micro", 0)

    def market_exposure_micro(self, market_id: str) -> int:
        """单市场敞口（micro USDC）：各腿净份额 × 当前中间价（MtM）求和；
        无中间价时按成本兜底【自拟口径，见模块头】。"""
        total = 0
        for token_id, pos in self._positions.get(market_id, {}).items():
            shares = pos.get("shares_micro", 0)
            if shares <= 0:
                continue
            mid = self._token_mid(token_id)
            if mid is not None:
                total += shares * mid // MICRO
            else:
                total += pos.get("cost_micro", 0)
        return total

    def total_exposure_micro(self) -> int:
        return sum(self.market_exposure_micro(m) for m in self._positions)

    def net_value_micro(self) -> int:
        """净值 = 账本现金 + 全部库存 MtM（§6.2/§12.2 按当前净值缩放口径）。"""
        return self.ledger.get_cash_balance_micro() + self.total_exposure_micro()

    def get_positions(self) -> Dict:
        return {m: {t: dict(p) for t, p in toks.items()}
                for m, toks in self._positions.items() if toks}

    def get_state(self, market_id: str) -> Dict:
        reg = self._markets[market_id]
        return {"state": reg.state, "reason": reg.state_reason,
                "quotable": reg.quotable, "band": reg.band, "score": reg.score}

    def _check_buy(self, market_id: str, add_amount_micro: int) -> tuple:
        """买入路径仓位校验（§6.2）：存量按 MtM、增量按金额（保守口径）。
        返回 (ok, reason)。报价与成交两条路径共用，超限由调用方计数。"""
        equity = self.net_value_micro()
        if equity <= 0:
            return False, "净值 ≤ 0"
        market_cap = equity * int(MAX_MARKET_EXPOSURE_PCT * 10) // 1000
        if self.market_exposure_micro(market_id) + add_amount_micro > market_cap:
            return False, f"单市场敞口超限（>{MAX_MARKET_EXPOSURE_PCT}% 净值，§6.2）"
        total_cap = equity * int(MAX_TOTAL_EXPOSURE_PCT * 10) // 1000
        if self.total_exposure_micro() + add_amount_micro > total_cap:
            return False, f"总库存敞口超限（>{MAX_TOTAL_EXPOSURE_PCT}% 净值，§6.2）"
        return True, ""

    # -- 状态机 -----------------------------------------------------------------

    def _hours_to_expiry(self, reg: _MarketReg, now: datetime) -> Optional[float]:
        if reg.end_dt is None:
            return None
        return (reg.end_dt - now).total_seconds() / 3600.0

    def _volatility_trending(self, market_id: str, now: datetime) -> bool:
        """窗口内 YES 中间价相对振幅 ≥ 阈值 → TRENDING【自拟度量】。"""
        hist = self._mid_hist.get(market_id, [])
        if len(hist) < 2:
            return False
        cutoff = now.timestamp() - SELF_VOL_WINDOW_SEC
        window = [mid for ts, mid in hist if ts >= cutoff]
        if len(window) < 2:
            return False
        mean = sum(window) / len(window)
        if mean <= 0:
            return False
        return (max(window) - min(window)) / mean * 100.0 >= SELF_VOL_TRENDING_PCT

    def _determine_state(self, market_id: str, now: datetime) -> tuple:
        """五态判定，优先级：HALTED > EVENT > REDUCE_ONLY > TRENDING > QUIET。
        转移依据逐条注节号；返回 (state, reason)。"""
        reg = self._markets[market_id]
        if self._halted_reason or self._hb_dead:
            return STATE_HALTED, self._halted_reason or "心跳 dead 闩锁"
        if self._breaker is not None and self._breaker(self):
            self.trip_halt("外部熔断回调触发（§6.3 注入）")
            return STATE_HALTED, self._halted_reason
        hours = self._hours_to_expiry(reg, now)
        # EVENT：注入事件窗口 / 事件日历回调 / 到期 48h 停开仓（§5.1-4、§11）
        if reg.event_window and reg.event_window[0] <= now <= reg.event_window[1]:
            return STATE_EVENT, "事件窗口内（注入日程，§11）"
        if self._event_provider is not None and self._event_provider(market_id, now):
            return STATE_EVENT, "事件日历回调命中（§11）"
        if hours is not None and hours < EXPIRY_STOP_OPEN_H:
            if hours < EXPIRY_CLEAR_H:
                return STATE_REDUCE_ONLY, f"到期前 {hours:.1f}h <6h：原则清零、只挂卖单（§6.2）"
            if hours < EXPIRY_REDUCE_H:
                return STATE_REDUCE_ONLY, f"到期前 {hours:.1f}h <24h：只减仓、库存降至 {EXPIRY_REDUCE_CAP_PCT:.0f}%（§6.2）"
            return STATE_EVENT, f"到期前 {hours:.1f}h <48h：停止新开仓（§5.1-4/§6.2）"
        if reg.reduce_only_signal:
            return STATE_REDUCE_ONLY, "信号注入只减仓（§11.3）"
        equity = self.net_value_micro()
        if equity > 0 and self.market_exposure_micro(market_id) >= \
                equity * int(MAX_MARKET_EXPOSURE_PCT * 10) // 1000:
            return STATE_REDUCE_ONLY, "单市场敞口已达上限（§6.2）"
        if self._volatility_trending(market_id, now):
            return STATE_TRENDING, f"窗口振幅 ≥{SELF_VOL_TRENDING_PCT}%（§11 TRENDING）"
        return STATE_QUIET, "常态"

    # -- 模拟撮合（CostModel） ---------------------------------------------------

    def _crossing_fill_micro(self, order: _RestingOrder, book: Dict) -> int:
        """穿越判定 + 本次可成交量（micro shares）。

        【自拟 CostModel v1，见交付说明】：
        - 触发：对手方最优价穿越我方限价（BUY：best_ask ≤ 我方价；
          SELL：best_bid ≥ 我方价）；
        - 穿越量：新快照中对手方价格穿越我方限价的全部档位量之和；
        - 排队折损：累计穿越量 × queue_haircut（单调累计，避免同一存量
          深度被多步重复计数）；本次增量 = 累计 − 已成交，封顶剩余量；
        - 成交价 = 我方限价（对我方最不利的合法成交价口径——穿越价
          更优部分不计入，保守）。
        """
        if order.side == "BUY":
            levels = book.get("asks") or []
            if not levels or _p2m(levels[0]["price"]) > order.price_micro:
                return 0
            through = sum(_s2m(lv["size"]) for lv in levels
                          if _p2m(lv["price"]) <= order.price_micro)
        else:
            levels = book.get("bids") or []
            if not levels or _p2m(levels[0]["price"]) < order.price_micro:
                return 0
            through = sum(_s2m(lv["size"]) for lv in levels
                          if _p2m(lv["price"]) >= order.price_micro)
        counted = int(through * self._queue_haircut)
        if counted > order.counted_through_micro:
            order.counted_through_micro = counted
        increment = order.counted_through_micro - order.filled_micro
        return max(0, min(order.remaining_micro, increment))

    def _apply_fill(self, order: _RestingOrder, fill_shares_micro: int,
                    mid_at_fill_micro: Optional[int], now: datetime) -> int:
        """成交落账：账本 fills + 现金流水 + 库存 + markout 任务登记。"""
        amount_micro = order.price_micro * fill_shares_micro // MICRO
        fee_micro = amount_micro * self._fee_bps // 10000 if self._fee_bps else 0
        fill_id = self.ledger.record_fill(
            order_id=order.ledger_order_id, market_id=order.market_id,
            token_id=order.token_id, side=order.side,
            price_micro=order.price_micro, shares_micro=fill_shares_micro,
            fee_micro=fee_micro)
        if order.side == "BUY":
            self.ledger.record_cash(kind="fill_settle", amount_micro=-amount_micro,
                                    ref=order.client_order_id, note="paper BUY 成交")
        else:
            self.ledger.record_cash(kind="fill_settle", amount_micro=amount_micro,
                                    ref=order.client_order_id, note="paper SELL 成交")
        if fee_micro:
            self.ledger.record_cash(kind="fee", amount_micro=-fee_micro,
                                    ref=order.client_order_id, note="paper 手续费")
        pos = self._positions.setdefault(order.market_id, {}).setdefault(
            order.token_id, {"shares_micro": 0, "cost_micro": 0})
        if order.side == "BUY":
            pos["shares_micro"] += fill_shares_micro
            pos["cost_micro"] += amount_micro
        else:
            pos["shares_micro"] -= fill_shares_micro
            pos["cost_micro"] -= amount_micro
        order.filled_micro += fill_shares_micro
        self.ledger.update_order_status(
            order.ledger_order_id,
            "filled" if order.remaining_micro <= 0 else "partially_filled")
        self.stats["fills"] += 1
        if mid_at_fill_micro is not None:
            self._markout_tasks.append({
                "fill_id": fill_id, "market_id": order.market_id,
                "token_id": order.token_id, "side": order.side,
                "mid_at_fill_micro": mid_at_fill_micro,
                "due_ms": int(now.timestamp() * 1000) + MARKOUT_HORIZON_SEC * 1000,
            })
        return fill_id

    def _evaluate_fills(self, market_id: str, books: Dict, now: datetime) -> List[Dict]:
        """对本市场在挂订单逐笔评估穿越成交（成交路径同样过仓位校验）。"""
        results = []
        for order in list(self._resting.values()):
            if order.market_id != market_id or order.remaining_micro <= 0:
                continue
            book = books.get(order.token_id)
            if book is None:
                continue
            fill_micro = self._crossing_fill_micro(order, book)
            if fill_micro <= 0:
                continue
            if order.side == "BUY":
                add_amount = order.price_micro * fill_micro // MICRO
                ok, reason = self._check_buy(market_id, add_amount)
                if not ok:
                    self.stats["fills_rejected_limit"] += 1
                    results.append({"order": order.client_order_id, "rejected": reason})
                    continue
            else:
                held = self.position_shares_micro(market_id, order.token_id)
                if fill_micro > held:
                    fill_micro = held
                    if fill_micro <= 0:
                        continue
            fill_id = self._apply_fill(order, fill_micro, _mid_micro(book), now)
            results.append({"order": order.client_order_id, "fill_id": fill_id,
                            "side": order.side, "shares_micro": fill_micro,
                            "price_micro": order.price_micro})
            if order.remaining_micro <= 0:
                del self._resting[order.client_order_id]
        return results

    # -- markout 采样（可独立调用） ----------------------------------------------

    def pending_markouts(self) -> List[Dict]:
        return [dict(t) for t in self._markout_tasks]

    def sample_markouts(self, now: Optional[datetime] = None,
                        books_by_market: Optional[Dict[str, Dict]] = None) -> List[Dict]:
        """对到期（成交后 ≥300s）的 markout 任务采样并写账本。

        books_by_market：{market_id: {token_id: Book}}；缺省用引擎最近
        一次 on_book 的快照。无可用盘口的任务保留待采并计数，不造假。
        """
        now = now or datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        sampled, remaining = [], []
        for task in self._markout_tasks:
            if task["due_ms"] > now_ms:
                remaining.append(task)
                continue
            books = (books_by_market or {}).get(task["market_id"]) \
                or self._last_books.get(task["market_id"]) or {}
            book = books.get(task["token_id"])
            mid_now = _mid_micro(book) if book else None
            if mid_now is None:
                self.stats["markouts_pending_no_book"] += 1
                remaining.append(task)
                continue
            sample_id = self.ledger.record_markout(
                fill_id=task["fill_id"], market_id=task["market_id"],
                token_id=task["token_id"], side=task["side"],
                mid_at_fill_micro=task["mid_at_fill_micro"],
                mid_at_horizon_micro=mid_now,
                horizon_sec=MARKOUT_HORIZON_SEC, source="paper_sim")
            self.stats["markouts_sampled"] += 1
            sampled.append({"sample_id": sample_id, **task, "mid_now_micro": mid_now})
        self._markout_tasks = remaining
        return sampled

    # -- 报价生成 -------------------------------------------------------------------

    def _tick_micro(self, reg: _MarketReg, book: Dict) -> int:
        tick = book.get("tick_size") or reg.market.get("tick_size")
        return _p2m(tick) if tick else _p2m(SELF_DEFAULT_TICK)

    def _place(self, reg: _MarketReg, token_id: str, side: str,
               price_micro: int, shares_micro: int) -> _RestingOrder:
        self._seq += 1
        client_id = f"paper-{reg.market['id']}-{self._seq}"
        ledger_id = self.ledger.record_order(
            client_order_id=client_id, market_id=str(reg.market["id"]),
            token_id=token_id, side=side, price_micro=price_micro,
            shares_micro=shares_micro,
            condition_id=reg.market.get("condition_id"), post_only=True,
            status="open")
        order = _RestingOrder(client_order_id=client_id, ledger_order_id=ledger_id,
                              market_id=str(reg.market["id"]), token_id=token_id,
                              side=side, price_micro=price_micro,
                              shares_micro=shares_micro)
        self._resting[client_id] = order
        self.stats["quotes_placed"] += 1
        return order

    def _cancel_market_orders(self, market_id: str) -> int:
        n = 0
        for cid, order in list(self._resting.items()):
            if order.market_id == market_id:
                self.ledger.update_order_status(order.ledger_order_id, "cancelled")
                del self._resting[cid]
                n += 1
        self.stats["cancels"] += n
        return n

    def _generate_quotes(self, reg: _MarketReg, books: Dict, state: str) -> List[Dict]:
        market_id = str(reg.market["id"])
        placed: List[Dict] = []
        if not reg.quotable or state == STATE_HALTED:
            return placed
        opening_allowed = state in (STATE_QUIET, STATE_TRENDING) and not self._hb_stale
        sell_allowed = state in (STATE_QUIET, STATE_TRENDING, STATE_EVENT, STATE_REDUCE_ONLY)
        size_factor = BAND_SIZE_FACTOR.get(reg.band or "", 0.0)
        if state == STATE_TRENDING:
            size_factor *= SELF_TRENDING_SIZE_MULT
        equity = self.net_value_micro()
        market_cap = equity * int(MAX_MARKET_EXPOSURE_PCT * 10) // 1000 if equity > 0 else 0

        tokens = reg.market.get("clob_token_ids") or []
        for token_id in tokens:
            book = books.get(token_id)
            if book is None:
                continue
            mid = _mid_micro(book)
            if mid is None:
                continue
            tick = self._tick_micro(reg, book)
            half = max(tick, int(mid * self._half_spread_pct / 100.0))
            if state == STATE_TRENDING:
                half = int(half * SELF_TRENDING_WIDEN_MULT)
            base_shares = int(reg.target_shares_micro * size_factor)

            # ---- BUY（开仓/补腿）：§6.2 偏斜 + 仓位校验 ----
            if opening_allowed and base_shares > 0:
                price = min(mid - half, _p2m(book["best_bid"]))
                leg_exp = self.position_shares_micro(market_id, token_id) * mid // MICRO
                leg_ratio = (leg_exp * 100.0 / market_cap) if market_cap > 0 else 0.0
                stop_leg = leg_ratio >= SKEW_STOP_PCT
                if leg_ratio >= SKEW_WIDEN_PCT:
                    price -= tick            # §6.2：该方向后撤一档
                price = (price // tick) * tick  # tick 对齐（§8）：买价向下取整
                price = max(tick, min(price, MICRO - tick))
                if not stop_leg:
                    amount = price * base_shares // MICRO
                    ok, _reason = self._check_buy(market_id, amount)
                    if ok:
                        o = self._place(reg, token_id, "BUY", price, base_shares)
                        placed.append({"order": o.client_order_id, "side": "BUY",
                                       "token_id": token_id, "price_micro": price,
                                       "shares_micro": base_shares})
                    else:
                        self.stats["quotes_blocked_limit"] += 1

            # ---- SELL（减仓/库存退出）：只卖持有份额 ----
            held = self.position_shares_micro(market_id, token_id)
            if sell_allowed and held > 0:
                sell_shares = min(held, base_shares) if base_shares > 0 else held
                price = max(mid + half, _p2m(book["best_ask"]))
                price = -((-price) // tick) * tick  # 卖价向上取整到 tick
                price = max(tick, min(price, MICRO - tick))
                o = self._place(reg, token_id, "SELL", price, sell_shares)
                placed.append({"order": o.client_order_id, "side": "SELL",
                               "token_id": token_id, "price_micro": price,
                               "shares_micro": sell_shares})
        return placed

    # -- 主步进 -----------------------------------------------------------------

    def _update_day_anchor(self, now: datetime) -> None:
        """§6.3 日亏熔断（最小实现）：以当日首步净值为锚，
        净值 ≤ 锚 ×(1−2%) → trip_halt。跨日自动换锚。"""
        today = now.date()
        if self._day_anchor_date != today:
            self._day_anchor_date = today
            self._day_anchor_equity = self.net_value_micro()
            return
        if self._day_anchor_equity and self._day_anchor_equity > 0:
            floor = self._day_anchor_equity * (1000 - int(DAILY_LOSS_HALT_PCT * 10)) // 1000
            if self.net_value_micro() <= floor and self._halted_reason is None:
                self.trip_halt(f"单日亏损达 −{DAILY_LOSS_HALT_PCT}%（§6.3 最小实现）")

    def on_book(self, market_id: str, books: Dict[str, Dict],
                now: Optional[datetime] = None) -> Dict:
        """喂入一个市场的一组盘口快照（{token_id: Book dict}），步进一拍：
        穿越成交 → markout 采样 → 日亏锚 → 状态判定 → 撤旧单 → 生成新报价。
        返回本拍报告（状态/成交/采样/新报价/拦截计数快照）。"""
        now = now or datetime.now(timezone.utc)
        market_id = str(market_id)
        reg = self._markets[market_id]
        self._last_books[market_id] = books
        for token_id, book in books.items():
            mid = _mid_micro(book)
            if mid is not None:
                self._last_mid[token_id] = mid
        tokens = reg.market.get("clob_token_ids") or []
        if tokens and tokens[0] in books:
            mid0 = _mid_micro(books[tokens[0]])
            if mid0 is not None:
                hist = self._mid_hist.setdefault(market_id, [])
                hist.append((now.timestamp(), mid0 / MICRO))
                cutoff = now.timestamp() - SELF_VOL_WINDOW_SEC * 4
                self._mid_hist[market_id] = [h for h in hist if h[0] >= cutoff]

        fills = self._evaluate_fills(market_id, books, now)
        sampled = self.sample_markouts(now, {market_id: books})
        self._update_day_anchor(now)
        state, reason = self._determine_state(market_id, now)
        reg.state, reg.state_reason = state, reason
        cancelled = self._cancel_market_orders(market_id)
        quotes = self._generate_quotes(reg, books, state) if state != STATE_HALTED else []
        return {
            "market_id": market_id, "state": state, "state_reason": reason,
            "fills": fills, "markouts_sampled": sampled, "cancelled": cancelled,
            "quotes": quotes,
            "net_value_micro": self.net_value_micro(),
            "market_exposure_micro": self.market_exposure_micro(market_id),
            "stats": dict(self.stats),
        }
