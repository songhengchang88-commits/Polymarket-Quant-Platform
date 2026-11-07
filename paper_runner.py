"""
paper_runner.py — 闸门 A 纸面长跑编排器（阶段 2：纸面同构长跑驱动）

定位：把既有五件串成可长跑的纸面验证回路——
sdk_adapter（公开只读行情）→ market_scoring（选市）→ paper_engine
（报价/模拟撮合/markout）→ mm_ledger（paper 账本）→ risk_breaker
（熔断判定）。只用公开行情 + 模拟成交，账本强制 paper 模式，
不下单、不用凭证、不连真实账户。

依据：《Polymarket 做市交 Poly 执行方案》
- §7 闸门 A：纸面同构 ≥2 周、≥30 个市场样本；扣全部费用后
  markout(5m) 中位数为正；模拟最大回撤 < 日亏损限额的 3 倍；
  零例账实不符。不过关的处置（调阈值重跑、最多 3 轮）归运营决策，
  本编排器只负责按口径采集可判定数据（见随附编排说明）；
- §5.1/§5.2 选市：否决剔除、按分数段选池（引擎 register 口径）；
  池新鲜度由缓存恢复后第 2 轮的全量重评 + 缓存 24h TTL 保证（每次
  重启即全量重选）；§5.2「按 markout 实测周度重评」属策略精修，
  已移入加固挂账（OI-10），原 weekly_reeval_stub 桩于 2026-11-07
  系统化清理中删除——不许留假函数在系统里冒充机制；
- §6.3 熔断：每轮以账本/引擎净值组装 BreakerSnapshot 交
  risk_breaker.evaluate，停机态经 engine.trip_halt / clear_halt
  接线（见 _apply_breaker 的接线口径说明）。

实现口径与适配登记（与既有模块的对接方式，均在此写明）：
1. rewards 数据：sdk_adapter.market_to_dict 未暴露 gamma 的 rewards
   字段（步 0/1 范围外），而 §5.1-1 在 rewards 缺失时按「政策不明」
   否决——故本编排器内置 SdkRewardsProvider，用 py-sdk 原生模型
   （Market.rewards.clob_rewards[].rewards_daily_rate）补取评分输入。
   这是编排层的补位，不改适配层；适配层日后扩展 rewards 字段后，
   本 provider 应退役并切换到适配层输出。
2. 引擎恢复：PaperEngine 无现成恢复接口（持仓/在挂单为内存态），
   本编排器在启动时做外部最小适配（restore_from_ledger）：
   - 持仓：回放账本 fills 重建 _positions（口径与引擎 _apply_fill
     的库存增减一致：BUY 加份额加成本、SELL 减份额减成本，金额用
     fills.amount_micro 整数推导值）；
   - 在挂单：引擎内存态已失，账本中 status=open/partially_filled
     的订单一律置 cancelled 并计数留痕（纸面单无交易所侧残留，
     不存在真实挂单风险）；
   - 待采 markout 任务：引擎内存态已失且成交时中间价不可考，
     不伪造补采——只统计「重启丢失的待采任务数」写入日志/日报。
   本适配只读账本 + 写回上述订单状态，不修改 paper_engine.py。
3. 熔断接线：runner 侧维护 engaged 标志——evaluate 返回
   can_open=False 时 engine.trip_halt（仅首次），返回 can_open=True
   且 engaged 时 engine.clear_halt。注意 clear_halt 同时清引擎的
   心跳 dead 闩锁与引擎内部日亏 halt：本编排器不接心跳（纸面期
   无真实挂单，心跳归步 5 实盘装配），且引擎内部日亏锚与
   risk_breaker 的日初净值口径同源（均为当日首见净值、−2%），
   两者在跨日恢复时点一致，冲突面已登记在编排说明。
4. 净值与快照：equity_now = engine.net_value_micro()（现金 + 库存
   按最近中间价 MtM）；equity_peak / day_start_equity 由本编排器
   持久化在 run_dir/equity_state.json（交易日按 Asia/Shanghai
   切分，与 risk_breaker 同口径）；reconcile_ok 为真实对账结果
   （2026-11-07 系统化接线，取代原纸面期恒 True 的假信号）：启动时
   与每 RECONCILE_EVERY_ROUNDS 轮以 reconcile_paper 的三表勾稽核
   账本内部一致性（orders/fills/cash/markout/成本恒等式），硬差异
   即置 False → RiskBreaker 进 RECONCILE_HALT 全停，恢复须人工
   clear（先对账后谈恢复，§6.3）。
5. 选池韧性（加固线 2，2026-11-07）：选池全程走进程内退避重试——
   启动选池无限重试（退避 5s 起、封顶 120s），瞬时网络错误不再
   整进程崩溃靠 systemd 重启（当日曾崩溃循环 16 次）；主循环另有
   轮级异常兜底与单市场 on_book 异常隔离（计该市场数据失败、走
   既有摘除逻辑），单点异常不杀长跑。
6. 选池缓存（加固线 2，2026-11-07）：选池成功后把入选市场全量
   落盘 run_dir/pool_cache.json；重启时缓存未过期（TTL 24h）且
   可报价入选数 ≥ 目标半数 → 先用缓存池立即恢复轮询（零网络
   依赖，首轮从 ~150s 压到秒级），随后第 2 轮起做一次新鲜重评
   替换池子（失败保留缓存池、每 20 轮重试）。缓存只是加速器：
   缺失/损坏/过期一律回退全量选池，口径不变。--no-pool-cache
   可整体关停。
7. markout 待采任务持久化（加固线 2，2026-11-07）：待采任务原
   只存引擎内存，重启必丢（已累计丢 3+ 个样本）。改为 runner
   每轮采样后把 engine.pending_markouts() 原子落盘
   run_dir/markout_tasks.json，启动恢复时回注引擎续采（剔除
   账本已有样本者防重复采样；到期超过 24h 的不再回注、按丢失
   计数）。选 runner 状态文件而非账本新表的理由：待采任务是
   过程状态而非账面事实，与 equity_state.json 同层；账本是闸门
   数据真相源、长跑期不动其 schema；且任务关键字段
   mid_at_fill 只存在于引擎内存，runner 经现成出口落盘零侵入。

目录布局（默认 run_dir = <repo>/../../stage1/paper_run）：
    mm_ledger_paper.sqlite3   paper 账本（MmLedger 落盘）
    risk_breaker_state.json   熔断状态（RiskBreaker 落盘）
    equity_state.json         净值峰值/日初锚（本编排器落盘）
    pool_events.jsonl         市场池变动事件（选池/摘除/恢复）
    pool_cache.json           选池缓存（加固线 2，机制见登记 6）
    markout_tasks.json        markout 待采任务（加固线 2，机制见登记 7）
    logs/run-YYYY-MM-DD.log   按日运行日志
    reports/report-YYYY-MM-DD.md  日报（daily_report 生成）
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from mm_ledger import MmLedger, micro_to_decimal
from paper_engine import PaperEngine
from reconcile_paper import reconcile_connection
from risk_breaker import BreakerSnapshot, RiskBreaker
from sdk_adapter import SdkReadOnlyAdapter
import market_scoring

__all__ = ["PaperRunner", "SdkRewardsProvider", "daily_report", "main"]

_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai，与 risk_breaker 同口径

# ---------------------------------------------------------------------------
# 【自拟】编排参数默认值（方案未给数值，编排说明登记，待长跑校准）
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL_SEC = 15.0   # REST 轮询间隔（任务书指定默认 15 秒）
DEFAULT_POOL_TARGET = 30           # 【方案 §7】闸门 A ≥30 个市场样本
DEFAULT_CANDIDATE_LIMIT = 150      # 候选池：按 24h 量取前 N 再评分筛选
DEFAULT_BULK_CHUNK_TOKENS = 50     # 批量盘口单次 token 数（分片拉取）
DEFAULT_MAX_CONSEC_FAILURES = 3    # 单市场连续失败达此数暂时摘除
DEFAULT_SUSPEND_ROUNDS = 20        # 摘除后多少轮尝试恢复
STALE_BOOK_MAX_SEC = 180           # 盘口服务端时间戳超龄判陈旧（A-10；轮询 15s，留足冗余）
RETRY_ATTEMPTS = 4                 # 批量拉取失败重试次数（自备退避，适配层无重试）
RETRY_BASE_DELAY_SEC = 1.0         # 指数退避基数：1s, 2s, 4s（+ 抖动）

# 【自拟】加固线 2 参数（2026-11-07，待长跑校准）
POOL_BUILD_BASE_DELAY_SEC = 5.0    # 选池重试退避基数：5s, 10s, 20s …（封顶下行）
POOL_BUILD_MAX_DELAY_SEC = 120.0   # 选池重试退避封顶
POOL_CACHE_FILE = "pool_cache.json"
POOL_CACHE_VERSION = 1
POOL_CACHE_TTL_SEC = 24 * 3600     # 选池缓存新鲜度：超过 24h 不用（市场会到期/退场）
POOL_CACHE_MIN_FRACTION = 0.5      # 缓存可报价入选 < 目标×此比例 → 弃缓存走全量选池
REEVAL_FIRST_ROUND = 2             # 缓存恢复后第几轮起做新鲜重评
REEVAL_RETRY_ROUNDS = 20           # 重评失败后隔多少轮再试
RECONCILE_EVERY_ROUNDS = 60        # 在线对账节拍：每 60 轮（约 15 分钟）真勾稽一次
MARKOUT_TASKS_FILE = "markout_tasks.json"
MARKOUT_TASKS_VERSION = 1
MARKOUT_TASKS_MAX_STALE_SEC = 24 * 3600  # 待采任务到期超此时长未采 → 不再回注、计丢失
                                         # （再采的中间价已非 300s 口径，采了反而污染样本）


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(dt: datetime) -> str:
    return dt.astimezone(_TZ).strftime("%Y-%m-%d")


def _day_bounds_ms(day_key: str) -> tuple:
    start = datetime.strptime(day_key, "%Y-%m-%d").replace(tzinfo=_TZ)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def call_with_backoff(fn, *, attempts: int = RETRY_ATTEMPTS,
                      base_delay: float = RETRY_BASE_DELAY_SEC,
                      sleep: Callable[[float], None] = time.sleep):
    """指数退避重试（适配层默认 10s 超时且无重试，编排层自备）。
    全部尝试失败后抛最后一次异常，由调用方按市场/批次计数处理。"""
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 行情拉取失败统一退避重试
            last_exc = exc
            if i < attempts - 1:
                sleep(base_delay * (2 ** i) + 0.1 * i)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# rewards 补取（模块头口径 1）
# ---------------------------------------------------------------------------

class RewardsFetchError(RuntimeError):
    """rewards 传输层取数失败（区别于「确认无奖励」的数据答案，A-03）。"""


REWARDS_CIRCUIT_BREAKER = 5   # 连续传输失败达此数后快速抛错（A-03）


class SdkRewardsProvider:
    """用 py-sdk 原生 gamma 模型补取市场 rewards（评分 §5.1-1/§5.2 输入）。

    返回 scoring 期望的 rewards dict：
    {"has_rewards": bool, "daily_reward_usd": float}
    三态语义见 get_rewards（A-03）：传输失败抛 RewardsFetchError，
    不再吞成 None 冒充「政策不明」。
    """

    def __init__(self, public_client=None):
        self._public = public_client
        self.fetch_errors = 0      # 累计传输失败次数（选池统计留痕）
        self._consec_errors = 0    # 连续失败（熔断快速失败用）

    def _client(self):
        if self._public is None:
            # 与适配层同口径：构造 SDK 客户端前清洗本进程 no_proxy 中的
            # [IPv6] 条目，否则 httpx 构造即抛 InvalidURL（适配层
            # _sanitize_no_proxy_env 的由来，见 sdk_adapter 模块头）。
            from sdk_adapter import _sanitize_no_proxy_env

            _sanitize_no_proxy_env()
            from polymarket import PublicClient  # py-sdk 0.16.0

            self._public = PublicClient()
        return self._public

    def get_rewards(self, market: Dict) -> Optional[Dict]:
        """三态语义（加固 A-03）：成功有奖励 → dict；成功确认无奖励 →
        has_rewards=False；查不到该市场 → None（数据答案）；传输失败 →
        重试后抛 RewardsFetchError。旧实现把传输失败也吞成 None，
        scoring 会按 §5.1-1「政策不明」否决——一次网络抖动即可清零
        候选集且留痕造假。连续失败达熔断阈值后快速抛错，避免 300 个
        候选逐个重试把选池拖死（选池层有自己的退避重试接住）。"""
        tokens = market.get("clob_token_ids") or []
        if not tokens:
            return None
        if self._consec_errors >= REWARDS_CIRCUIT_BREAKER:
            raise RewardsFetchError(
                f"rewards 取数连续失败 {self._consec_errors} 次，熔断快速失败")
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                paginator = self._client().list_markets(
                    clob_token_ids=str(tokens[0]), page_size=1)
                items = paginator.first_page().items
                break
            except Exception as exc:  # noqa: BLE001 — 传输类失败重试
                last_exc = exc
                self.fetch_errors += 1
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        else:
            self._consec_errors += 1
            raise RewardsFetchError(
                f"rewards 取数 3 次重试失败：{last_exc!r}") from last_exc
        self._consec_errors = 0
        if not items:
            return None
        rewards = items[0].rewards
        clob_rewards = list(rewards.clob_rewards or []) if rewards else []
        if not clob_rewards:
            return {"has_rewards": False, "daily_reward_usd": 0.0}
        daily = sum(float(r.rewards_daily_rate) for r in clob_rewards)
        return {"has_rewards": True, "daily_reward_usd": daily}


# ---------------------------------------------------------------------------
# 净值锚持久化（模块头口径 4）
# ---------------------------------------------------------------------------

class EquityTracker:
    """净值峰值与日初锚：落盘 run_dir/equity_state.json。"""

    def __init__(self, run_dir: str):
        self.path = os.path.join(run_dir, "equity_state.json")
        self.peak_micro = 0
        self.day_start: Dict[str, int] = {}
        self.load_error = False
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("equity state is not a JSON object")
                self.peak_micro = int(raw.get("peak_micro", 0))
                self.day_start = {k: int(v) for k, v
                                  in raw.get("day_start", {}).items()}
            except (OSError, ValueError, TypeError):
                # 文件损坏容错（系统化补漏，与 risk_breaker L1-10 同口径）：
                # 旧实现裸 json.load 抛错会让 runner 启动即崩、systemd
                # 死循环。改名留证 + 空状态重建（首轮 observe 以当前净值
                # 重设峰值/日锚）+ load_error 标记供 runner 记日志告警。
                try:
                    os.replace(self.path,
                               self.path + f".corrupt-{int(time.time())}")
                except OSError:
                    pass
                self.peak_micro = 0
                self.day_start = {}
                self.load_error = True

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"peak_micro": self.peak_micro, "day_start": self.day_start},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def observe(self, equity_micro: int, now: datetime) -> tuple:
        """记录一次净值观测，返回 (peak_micro, day_start_equity_micro)。"""
        changed = False
        if equity_micro > self.peak_micro:
            self.peak_micro = equity_micro
            changed = True
        key = _day_key(now)
        if key not in self.day_start:
            self.day_start[key] = equity_micro
            changed = True
        if changed:
            self._save()
        return self.peak_micro, self.day_start[key]


# ---------------------------------------------------------------------------
# 编排器主体
# ---------------------------------------------------------------------------

class PaperRunner:
    """闸门 A 纸面长跑编排器（步进轮询驱动，无独立线程）。"""

    def __init__(self, run_dir: str, *,
                 adapter: Optional[SdkReadOnlyAdapter] = None,
                 rewards_provider: Optional[SdkRewardsProvider] = None,
                 pool_target: int = DEFAULT_POOL_TARGET,
                 candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
                 poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
                 seed_usd: str = "200",
                 base_shares: str = "20",
                 max_consec_failures: int = DEFAULT_MAX_CONSEC_FAILURES,
                 suspend_rounds: int = DEFAULT_SUSPEND_ROUNDS,
                 use_pool_cache: bool = True):
        self.run_dir = run_dir
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "reports"), exist_ok=True)
        self.adapter = adapter if adapter is not None else SdkReadOnlyAdapter()
        self.rewards_provider = rewards_provider if rewards_provider is not None \
            else SdkRewardsProvider()
        self.pool_target = int(pool_target)
        self.candidate_limit = int(candidate_limit)
        self.poll_interval_sec = float(poll_interval_sec)
        self.max_consec_failures = int(max_consec_failures)
        self.suspend_rounds = int(suspend_rounds)
        self.use_pool_cache = bool(use_pool_cache)

        self.ledger = MmLedger("paper", run_dir)
        self.breaker = RiskBreaker(run_dir)
        self.tracker = EquityTracker(run_dir)
        self.engine = PaperEngine(self.ledger, seed_usd=seed_usd,
                                  base_shares=base_shares,
                                  state_file=os.path.join(
                                      run_dir, "engine_state.json"))

        self.pool: Dict[str, Dict] = {}        # market_id → 注册信息
        self._fail_count: Dict[str, int] = {}
        self._suspended_until: Dict[str, int] = {}   # market_id → 恢复轮次
        self._breaker_engaged = False
        self._round = 0
        self._pool_reeval_pending = False      # 缓存恢复后待新鲜重评（登记 6）
        self._reeval_next_round = 0            # 下次允许重评的轮次
        self._consec_round_errors = 0          # 连续轮级异常计数（登记 5）
        self.restore_info = self.restore_from_ledger()
        self._log(f"runner 启动：run_dir={run_dir} 恢复={self.restore_info}")
        if self.tracker.load_error:
            self._log("警告：equity_state.json 损坏已改名留证，峰值/日锚"
                      "以当前净值重建（本日日亏口径以 RiskBreaker 落盘为准）")
        # 在线对账（系统化接线）：启动先勾稽一次，此后每
        # RECONCILE_EVERY_ROUNDS 轮刷新，结果喂 RiskBreaker。
        self._reconcile_ok = True
        self._reconcile_hard: List[str] = []
        self._reconcile_failures = 0
        self._last_reconcile_round = 0
        self._refresh_reconcile()

    # -- 日志与池事件 ---------------------------------------------------------

    def _log(self, msg: str) -> None:
        now = _now_utc()
        line = f"{now.isoformat()} {msg}"
        print(line, flush=True)
        path = os.path.join(self.run_dir, "logs", f"run-{_day_key(now)}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _pool_event(self, event: Dict) -> None:
        event = {"at": _now_utc().isoformat(), **event}
        path = os.path.join(self.run_dir, "pool_events.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._log(f"池事件：{json.dumps(event, ensure_ascii=False)}")

    # -- markout 待采任务持久化（模块头登记 7） --------------------------------

    def _markout_tasks_path(self) -> str:
        return os.path.join(self.run_dir, MARKOUT_TASKS_FILE)

    def _persist_markout_tasks(self) -> None:
        """把引擎当前待采任务原子落盘。落盘失败只记日志（下一轮再写）——
        持久化是防丢样本的保险丝，不能反过来杀长跑。"""
        try:
            data = {"version": MARKOUT_TASKS_VERSION,
                    "saved_at_ms": int(time.time() * 1000),
                    "tasks": self.engine.pending_markouts()}
            path = self._markout_tasks_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"markout 待采任务落盘失败（下一轮重试）：{exc!r}")

    def _load_markout_tasks(self) -> List[Dict]:
        """读回落盘的待采任务；文件缺失/损坏/版本不符一律按空处理
        （回到旧行为：无任务可恢复，丢失由 restore 计数口径兜底）。"""
        path = self._markout_tasks_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # noqa: BLE001
            self._log(f"markout 待采任务文件损坏，按空处理：{exc!r}")
            return []
        if raw.get("version") != MARKOUT_TASKS_VERSION:
            self._log(f"markout 待采任务文件版本不符（{raw.get('version')}），按空处理")
            return []
        tasks = []
        for t in raw.get("tasks") or []:
            try:
                tasks.append({
                    "fill_id": int(t["fill_id"]),
                    "market_id": str(t["market_id"]),
                    "token_id": str(t["token_id"]),
                    "side": str(t["side"]),
                    "mid_at_fill_micro": int(t["mid_at_fill_micro"]),
                    "due_ms": int(t["due_ms"]),
                })
            except (KeyError, TypeError, ValueError):
                continue   # 单条畸形不拖垮其余任务
        return tasks

    # -- 启动恢复（模块头口径 2） ----------------------------------------------

    def restore_from_ledger(self) -> Dict:
        """从账本重建引擎持仓；账本残留在挂单置 cancelled；待采 markout
        任务先从落盘文件回注续采（登记 7），回注之外的丢失只计数不补造。
        引擎新进程构造后调用一次。"""
        positions: Dict[str, Dict[str, Dict[str, int]]] = {}
        sampled_fill_ids = {s["fill_id"] for s in self.ledger.get_markout_samples()}
        now_ms = int(time.time() * 1000)

        # 待采任务回注：剔除账本已有样本者（防重复采样——落盘后、采样前
        # 崩溃会留下这种陈旧条目）；剔除到期已超 MARKOUT_TASKS_MAX_STALE_SEC
        # 者（其 fill 由下方丢失口径计数）；同 fill_id 只留一条。
        restored_tasks: List[Dict] = []
        seen_fill_ids = set()
        for task in self._load_markout_tasks():
            fid = task["fill_id"]
            if fid in sampled_fill_ids or fid in seen_fill_ids:
                continue
            if now_ms - task["due_ms"] > MARKOUT_TASKS_MAX_STALE_SEC * 1000:
                continue
            seen_fill_ids.add(fid)
            restored_tasks.append(task)
        self.engine._markout_tasks.extend(restored_tasks)
        restored_fill_ids = {t["fill_id"] for t in restored_tasks}

        lost_markouts = 0
        horizon_ms = 300 * 1000
        for fill in self.ledger.get_fills():
            pos = positions.setdefault(fill["market_id"], {}).setdefault(
                fill["token_id"], {"shares_micro": 0, "cost_micro": 0})
            if fill["side"] == "BUY":
                pos["shares_micro"] += fill["shares_micro"]
                pos["cost_micro"] += fill["amount_micro"]
            else:
                # 平均成本扣减，与引擎 _apply_fill 同口径（加固 L1-02）：
                # 按成交额扣会让回放出的成本基与真实成本不一致甚至转负。
                held_before = pos["shares_micro"]
                if held_before > 0:
                    if fill["shares_micro"] >= held_before:
                        reduction = pos["cost_micro"]
                    else:
                        reduction = (pos["cost_micro"] * fill["shares_micro"]
                                     // held_before)
                    pos["cost_micro"] -= max(0, min(reduction,
                                                     pos["cost_micro"]))
                pos["shares_micro"] -= fill["shares_micro"]
            # 丢失口径（2026-11-07 修正）：无样本、无落盘任务的 fill 即
            # 不可恢复地丢失——mid_at_fill 只存在于引擎内存、无从重建。
            # 旧口径带「年龄 ≥ horizon」条件，使重启时未到期的任务丢失
            # 被两头落空（既不回注也不计数，实测 fills 9/10/11 漏计、
            # 对账 D6 亮硬差异）。新纪元下每轮落盘后被硬杀的残余窗口
            # 也由此精确计数，不再静默。
            if fill["id"] not in sampled_fill_ids and \
                    fill["id"] not in restored_fill_ids:
                lost_markouts += 1
        # 只保留有净持仓的市场/腿（与引擎 _positions 的存量口径一致）
        restored = {
            m: {t: p for t, p in toks.items() if p["shares_micro"] != 0}
            for m, toks in positions.items()
        }
        self.engine._positions = {m: toks for m, toks in restored.items() if toks}
        stale = 0
        for order in self.ledger.get_open_orders():
            self.ledger.update_order_status(order["id"], "cancelled")
            stale += 1
        return {"positions_restored": sum(len(t) for t in self.engine._positions.values()),
                "stale_orders_cancelled": stale,
                "markout_tasks_restored": len(restored_tasks),
                "markout_tasks_lost": lost_markouts}

    # -- 市场池 -----------------------------------------------------------------

    def _fetch_books_bulk(self, token_ids: List[str]) -> Dict[str, Dict]:
        """分片批量拉盘口（退避重试）；某片全失败时该片 token 缺席，
        由调用方按市场计失败。"""
        out: Dict[str, Dict] = {}
        for i in range(0, len(token_ids), DEFAULT_BULK_CHUNK_TOKENS):
            chunk = token_ids[i:i + DEFAULT_BULK_CHUNK_TOKENS]
            try:
                out.update(call_with_backoff(
                    lambda c=chunk: self.adapter.get_bulk_orderbooks(c)))
            except Exception as exc:  # noqa: BLE001
                self._log(f"批量盘口分片失败（{len(chunk)} token）：{exc!r}")
        return out

    def build_pool(self) -> Dict:
        """选池：适配层市场列表 → 逐个评分（否决剔除）→ 按分数降序
        取 quotable 至 pool_target，注册进引擎。返回选池统计。"""
        markets = call_with_backoff(
            lambda: self.adapter.list_top_markets(limit=self.candidate_limit))
        self._log(f"候选市场 {len(markets)} 个（candidate_limit={self.candidate_limit}）")
        # 评分需要两腿盘口：先批量拉全部候选的两腿
        all_tokens: List[str] = []
        for m in markets:
            all_tokens.extend(m.get("clob_token_ids") or [])
        books = self._fetch_books_bulk(all_tokens)

        scored: List[tuple] = []
        vetoed = 0
        for m in markets:
            mbooks = {t: books[t] for t in (m.get("clob_token_ids") or []) if t in books}
            # rewards 传输失败抛 RewardsFetchError → 整池构建按失败重试
            # （A-03），不许在此吞掉变成满池「政策不明」假否决。
            rewards = self.rewards_provider.get_rewards(m)
            result = market_scoring.score_market(
                m, books=mbooks or None, rewards=rewards)
            if result["vetoed"]:
                vetoed += 1
                continue
            # 排序与 band 同口径（A-01）：按 score_ratio（得分/可达满分）
            scored.append((result.get("score_ratio") or 0.0, m, result))
        scored.sort(key=lambda x: x[0], reverse=True)

        selected = 0
        for score, m, result in scored:
            if selected >= self.pool_target:
                break
            reg = self.engine.register_market(m, score_result=result)
            if not reg["quotable"]:
                continue
            self.pool[str(m["id"])] = {
                "market": m, "score": result["score"], "band": result["band"],
                "score_ratio": result.get("score_ratio"),
                "coverage": result.get("coverage"),
                "registered_at": _now_utc().isoformat(),
            }
            selected += 1
        info = {"candidates": len(markets), "vetoed": vetoed,
                "scored_ok": len(scored), "selected": selected,
                "pool_target": self.pool_target,
                "rewards_fetch_errors": getattr(
                    self.rewards_provider, "fetch_errors", 0),
                "shortfall": max(0, self.pool_target - selected)}
        self._pool_event({"type": "pool_built", **info,
                          "market_ids": list(self.pool.keys())})
        if info["shortfall"]:
            self._log(f"市场池不足：目标 {self.pool_target}，实际入选 {selected}"
                      f"（候选 {len(markets)}、否决 {vetoed}、评分通过 {len(scored)}）")
        self._save_pool_cache()
        return info

    # -- 选池韧性与缓存（模块头登记 5/6） --------------------------------------

    def _build_pool_with_retries(self, *, attempts: Optional[int] = None,
                                 base_delay: float = POOL_BUILD_BASE_DELAY_SEC,
                                 max_delay: float = POOL_BUILD_MAX_DELAY_SEC,
                                 sleep: Callable[[float], None] = time.sleep) -> Dict:
        """选池退避重试：attempts=None 为无限重试（启动路径）——长跑进程
        不因瞬时网络错误退出，宁可在进程内等网络恢复；有限次版供重评
        路径用（失败保留现有池子）。KeyboardInterrupt 不吞。"""
        n = 0
        while True:
            n += 1
            try:
                return self.build_pool()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — 选池失败统一退避
                if attempts is not None and n >= attempts:
                    raise
                # 指数封顶（系统化补漏）：旧式 base*(2**(n-1)) 在 min() 截断
                # 之前先算大整数，无限重试约千次后 int→float 溢出崩溃，
                # 且崩溃信息与真实失败原因无关。
                delay = min(max_delay, base_delay * (2 ** min(n - 1, 10)))
                self._log(f"选池第 {n} 次尝试失败：{exc!r}；{delay:.0f}s 后重试")
                sleep(delay)

    def build_pool_resilient(self) -> Dict:
        """启动选池（无限退避版，见 _build_pool_with_retries）。"""
        return self._build_pool_with_retries(attempts=None)

    def _pool_cache_path(self) -> str:
        return os.path.join(self.run_dir, POOL_CACHE_FILE)

    def _save_pool_cache(self) -> None:
        """选池成功后落盘缓存（登记 6）。落盘失败只记日志：缓存是
        加速器不是真相源，缺了它只是回到全量选池。"""
        try:
            data = {"version": POOL_CACHE_VERSION,
                    "built_at": _now_utc().isoformat(),
                    "built_at_ms": int(time.time() * 1000),
                    "pool_target": self.pool_target,
                    "entries": [
                        {"market": v["market"], "score": v["score"],
                         "band": v["band"],
                         "score_ratio": v.get("score_ratio"),
                         "coverage": v.get("coverage")}
                        for v in self.pool.values()]}
            path = self._pool_cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            self._log(f"选池缓存已落盘：{len(data['entries'])} 个市场")
        except Exception as exc:  # noqa: BLE001
            self._log(f"选池缓存落盘失败（不影响运行）：{exc!r}")

    @staticmethod
    def _parse_iso_dt(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _restore_pool_from_cache(self) -> bool:
        """用选池缓存恢复池子（登记 6）：零网络依赖。成功置
        _pool_reeval_pending，主循环随后做新鲜重评替换。
        失效规则（任一命中即弃缓存走全量选池）：文件缺失/损坏/
        版本不符；age > POOL_CACHE_TTL_SEC；条目已到期（end_date
        已过）；可报价入选数 < pool_target × POOL_CACHE_MIN_FRACTION
        （此时回滚本次注册的池子——引擎侧注册残留无害，喂数只看
        self.pool）。"""
        path = self._pool_cache_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # noqa: BLE001
            self._log(f"选池缓存文件损坏，弃缓存走全量选池：{exc!r}")
            return False
        if raw.get("version") != POOL_CACHE_VERSION:
            self._log(f"选池缓存版本不符（{raw.get('version')}），弃缓存")
            return False
        age_sec = time.time() - int(raw.get("built_at_ms", 0)) / 1000.0
        if age_sec > POOL_CACHE_TTL_SEC:
            self._log(f"选池缓存已过期（age {age_sec / 3600:.1f}h > "
                      f"{POOL_CACHE_TTL_SEC / 3600:.0f}h），弃缓存走全量选池")
            return False
        now = _now_utc()
        selected = 0
        for entry in raw.get("entries") or []:
            if selected >= self.pool_target:
                break
            m = entry.get("market") or {}
            mid = str(m.get("id") or "")
            tokens = m.get("clob_token_ids") or []
            if not mid or len(tokens) < 2:
                continue
            end_dt = self._parse_iso_dt(m.get("end_date") or "")
            if end_dt is not None and end_dt <= now:
                continue   # 缓存期间已到期
            reg = self.engine.register_market(
                m, score_result={"vetoed": False,
                                 "score": entry.get("score"),
                                 "band": entry.get("band"),
                                 "score_ratio": entry.get("score_ratio")})
            if not reg["quotable"]:
                continue
            self.pool[mid] = {"market": m, "score": entry.get("score"),
                              "band": entry.get("band"),
                              "score_ratio": entry.get("score_ratio"),
                              "coverage": entry.get("coverage"),
                              "registered_at": now.isoformat(),
                              "from_cache": True}
            selected += 1
        min_needed = max(1, math.ceil(self.pool_target * POOL_CACHE_MIN_FRACTION))
        if selected < min_needed:
            self._log(f"选池缓存可报价入选 {selected} < 下限 {min_needed}，弃缓存走全量选池")
            self.pool = {}
            return False
        self._pool_reeval_pending = True
        self._reeval_next_round = REEVAL_FIRST_ROUND
        self._pool_event({"type": "pool_restored_from_cache",
                          "selected": selected,
                          "cache_age_sec": int(age_sec),
                          "pool_target": self.pool_target})
        return True

    def _attempt_pool_reeval(self) -> None:
        """缓存池的新鲜重评替换（登记 6）：全量选池成功 → 换池并清理
        离池市场的失败/摘除记账；失败 → 恢复缓存池、隔
        REEVAL_RETRY_ROUNDS 轮再试。重评本身走有限次退避（3 次），
        不阻塞主循环到无限等待。"""
        old_pool = self.pool
        self.pool = {}
        try:
            info = self._build_pool_with_retries(attempts=3)
        except Exception as exc:  # noqa: BLE001
            self.pool = old_pool
            self._reeval_next_round = self._round + REEVAL_RETRY_ROUNDS
            self._log(f"选池重评失败，保留缓存池，{REEVAL_RETRY_ROUNDS} 轮后再试：{exc!r}")
            return
        for mid in list(self._fail_count):
            if mid not in self.pool:
                del self._fail_count[mid]
        for mid in list(self._suspended_until):
            if mid not in self.pool:
                del self._suspended_until[mid]
        self._pool_reeval_pending = False
        self._pool_event({"type": "pool_reevaluated", **info,
                          "market_ids": list(self.pool.keys())})

    # -- 在线对账（系统化接线，取代恒 True 假信号） ------------------------------

    def _refresh_reconcile(self) -> None:
        """以 reconcile_paper 三表勾稽核账本内部一致性，结果存
        self._reconcile_ok 供 _apply_breaker 喂 RiskBreaker。
        检查器自身连续 3 次异常按「账实不明」置 False（检查器坏掉
        本身就是不可信状态，不许静默放行）。"""
        self._last_reconcile_round = self._round
        try:
            result = reconcile_connection(self.ledger._conn)
        except Exception as exc:  # noqa: BLE001
            self._reconcile_failures += 1
            self._log(f"在线对账异常（连续 {self._reconcile_failures} 次）："
                      f"{exc!r}")
            if self._reconcile_failures >= 3 and self._reconcile_ok:
                self._reconcile_ok = False
                self._reconcile_hard = [f"对账检查器连续异常：{exc!r}"]
                self._log("在线对账：检查器连续 3 次异常，按账实不明置 "
                          "reconcile_ok=False（将触发 RECONCILE_HALT）")
            return
        self._reconcile_failures = 0
        new_ok = bool(result["reconcile_ok"])
        if new_ok != self._reconcile_ok or (not new_ok and
                                           result["hard_diffs"] != self._reconcile_hard):
            self._log(f"在线对账：reconcile_ok {self._reconcile_ok} → {new_ok}；"
                      f"硬差异={result['hard_diffs']}")
        self._reconcile_ok = new_ok
        self._reconcile_hard = list(result["hard_diffs"])

    # -- 熔断接线（模块头口径 3/4） ----------------------------------------------

    def _apply_breaker(self, now: datetime) -> Dict:
        if self._round - self._last_reconcile_round >= RECONCILE_EVERY_ROUNDS:
            self._refresh_reconcile()
        equity = self.engine.net_value_micro()
        peak, day_start = self.tracker.observe(equity, now)
        snap = BreakerSnapshot(
            equity_now_micro=equity,
            equity_peak_micro=peak,
            day_start_equity_micro=day_start,
            reconcile_ok=self._reconcile_ok,   # 真实在线对账结果，见 _refresh_reconcile
        )
        decision = self.breaker.evaluate(
            snap, now_ms=int(now.timestamp() * 1000))
        if not decision.can_open and not self._breaker_engaged:
            self.engine.trip_halt(f"risk_breaker[{decision.state}]：{decision.reason}")
            self._breaker_engaged = True
            self._log(f"熔断接线：trip_halt state={decision.state} {decision.reason}")
        elif decision.can_open and self._breaker_engaged:
            self.engine.clear_halt()
            self._breaker_engaged = False
            self._log("熔断接线：clear_halt（breaker 恢复 can_open）")
        return {"state": decision.state, "can_open": decision.can_open,
                "reason": decision.reason, "equity_micro": equity}

    # -- 驱动循环 -----------------------------------------------------------------

    def _active_market_ids(self) -> List[str]:
        return [mid for mid in self.pool
                if self._suspended_until.get(mid, 0) <= self._round]

    def step_round(self) -> Dict:
        """一轮：批量拉池内市场两腿盘口 → 逐市场喂引擎 → 全局 markout
        采样 → 熔断判定。返回本轮统计。"""
        self._round += 1
        now = _now_utc()
        active = self._active_market_ids()
        token_owner: Dict[str, str] = {}
        for mid in active:
            for t in self.pool[mid]["market"].get("clob_token_ids") or []:
                token_owner[t] = mid
        books = self._fetch_books_bulk(list(token_owner.keys()))

        fed, fills, quotes = 0, 0, 0
        failed_markets = set()
        for mid in active:
            tokens = self.pool[mid]["market"].get("clob_token_ids") or []
            mbooks = {t: books[t] for t in tokens if t in books}
            # 两腿缺失、双腿空盘或盘口陈旧 → 本市场本轮数据失败
            # （陈旧检测 A-10：此前全链无人消费服务端 timestamp，
            # 冻结盘口会被当实时数据喂进引擎）。
            now_ts = now.timestamp()
            stale = any(
                b.get("timestamp") is not None
                and now_ts - b["timestamp"] > STALE_BOOK_MAX_SEC
                for b in mbooks.values())
            if len(mbooks) < len(tokens) or stale or \
                    all(b.get("depth_count", 0) == 0 for b in mbooks.values()):
                failed_markets.add(mid)
                continue
            try:
                report = self.engine.on_book(mid, mbooks, now)
            except Exception as exc:  # noqa: BLE001 — 单市场异常隔离（登记 5）：
                # 畸形盘口/引擎单点异常只计该市场数据失败（走既有摘除
                # 逻辑），不许杀整轮、更不许杀长跑进程。
                self._log(f"市场 {mid} on_book 异常，本轮计数据失败：{exc!r}")
                failed_markets.add(mid)
                continue
            fed += 1
            fills += len([f for f in report["fills"] if "fill_id" in f])
            quotes += len(report["quotes"])
            self._fail_count[mid] = 0
        for mid in failed_markets:
            self._fail_count[mid] = self._fail_count.get(mid, 0) + 1
            if self._fail_count[mid] >= self.max_consec_failures:
                self._suspended_until[mid] = self._round + self.suspend_rounds
                self._fail_count[mid] = 0
                self._pool_event({"type": "market_suspended", "market_id": mid,
                                  "reason": "连续数据失败达阈值",
                                  "resume_round": self._suspended_until[mid]})
        # 恢复到期的摘除市场
        for mid, until in list(self._suspended_until.items()):
            if until <= self._round and mid in self.pool:
                del self._suspended_until[mid]
                self._pool_event({"type": "market_resumed", "market_id": mid})

        # 持仓中但已掉出池的市场：只喂估值盘口、不报价（加固线 3 净值
        # 伪影修复）。否则其 _last_mid 缺失、估值走成本兜底，重启换池时
        # 净值出现 −1.245 量级的口径跳变（可达日熔断阈值的近 1/3）。
        held_extra = [m for m in self.engine._positions if m not in active]
        if held_extra:
            extra_tokens: Dict[str, str] = {}
            for m in held_extra:
                for t in self.engine._positions[m]:
                    extra_tokens[t] = m
            ebooks = self._fetch_books_bulk(list(extra_tokens.keys()))
            by_market: Dict[str, Dict] = {}
            for t, m in extra_tokens.items():
                if t in ebooks:
                    by_market.setdefault(m, {})[t] = ebooks[t]
            for m, mb in by_market.items():
                self.engine.note_books(m, mb)

        self.engine.sample_markouts(now)
        self._persist_markout_tasks()   # 登记 7：采样后落盘待采任务（防重启丢样本）
        breaker_info = self._apply_breaker(now)
        stats = {"round": self._round, "active": len(active), "fed": fed,
                 "fills": fills, "quotes": quotes,
                 "failed": len(failed_markets), "breaker": breaker_info,
                 "engine_stats": dict(self.engine.stats)}
        self._log(f"第 {self._round} 轮：喂入 {fed}/{len(active)} 市场，"
                  f"成交 {fills}，新报价 {quotes}，失败 {len(failed_markets)}，"
                  f"熔断 {breaker_info['state']}，"
                  f"净值 {micro_to_decimal(breaker_info['equity_micro'])} USDC")
        return stats

    def run(self, *, rounds: Optional[int] = None,
            duration_sec: Optional[float] = None) -> Dict:
        """长跑主循环：rounds 与 duration_sec 至少给一个（都给时先到先停）。

        启动顺序（登记 5/6）：池为空时先试选池缓存（命中即零网络
        恢复轮询 + 待重评），未命中走无限退避全量选池——启动路径
        不因网络错误退出进程。主循环每轮有异常兜底（登记 5）。"""
        if not self.pool:
            restored = False
            if self.use_pool_cache:
                try:
                    restored = self._restore_pool_from_cache()
                except Exception as exc:  # noqa: BLE001 — 缓存只是加速器，
                    # 任何异常都回退全量选池，绝不让缓存杀启动路径
                    self.pool = {}
                    self._log(f"选池缓存恢复异常，弃缓存走全量选池：{exc!r}")
            if not restored:
                self.build_pool_resilient()
        started = time.time()
        last_report_day = _day_key(_now_utc())
        summary: Dict = {"rounds_done": 0}
        try:
            while True:
                t0 = time.time()
                try:
                    stats = self.step_round()
                    self._consec_round_errors = 0
                    summary = {**stats, "rounds_done": self._round}
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001 — 轮级兜底（登记 5）
                    self._consec_round_errors += 1
                    self._log(f"第 {self._round} 轮未捕获异常（连续 "
                              f"{self._consec_round_errors} 次），跳过本轮继续：\n"
                              f"{traceback.format_exc()}")
                    summary = {"rounds_done": self._round, "round_error": True}
                if self._pool_reeval_pending and \
                        self._round >= self._reeval_next_round:
                    self._attempt_pool_reeval()
                today = _day_key(_now_utc())
                if today != last_report_day:
                    daily_report(self.run_dir, last_report_day)
                    last_report_day = today
                if rounds is not None and self._round >= rounds:
                    break
                if duration_sec is not None and time.time() - started >= duration_sec:
                    break
                elapsed = time.time() - t0
                time.sleep(max(0.0, self.poll_interval_sec - elapsed))
        except KeyboardInterrupt:
            self._log("收到 KeyboardInterrupt，优雅停机")
        finally:
            self._persist_markout_tasks()   # 退出前最后落盘一次（登记 7）
        summary["pool_size"] = len(self.pool)
        summary["restore_info"] = self.restore_info
        return summary


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------

def daily_report(run_dir: str, day_key: Optional[str] = None) -> str:
    """生成某交易日（Asia/Shanghai）的纸面日报 markdown，返回文件路径。

    数据全部取自账本与落盘状态：成交、手续费、markout 样本取账本实数；
    净值锚与峰值取 equity_state.json；熔断状态取 risk_breaker 落盘；
    市场池变动取 pool_events.jsonl。当日盈亏不在此单列现金口径数字——
    纸面净值含库存 MtM，须以运行期 equity 序列为准（见报告内口径注）。
    """
    day_key = day_key or _day_key(_now_utc())
    start_ms, end_ms = _day_bounds_ms(day_key)
    ledger = MmLedger("paper", run_dir)
    try:
        fills = [f for f in ledger.get_fills()
                 if start_ms <= f["filled_at_ms"] < end_ms]
        samples = [s for s in ledger.get_markout_samples()
                   if start_ms <= s["sampled_at_ms"] < end_ms]
        all_samples = ledger.get_markout_samples()
        cash = ledger.get_cash_balance_micro()
        total_fills = len(ledger.get_fills())
    finally:
        ledger.close()

    def _median(values: List[int]) -> Optional[int]:
        if not values:
            return None
        vs = sorted(values)
        n = len(vs)
        return vs[n // 2] if n % 2 == 1 else (vs[n // 2 - 1] + vs[n // 2]) // 2

    day_median = _median([s["markout_micro"] for s in samples])
    all_median = _median([s["markout_micro"] for s in all_samples])
    day_fees = sum(f["fee_micro"] for f in fills)

    equity_state = {}
    eq_path = os.path.join(run_dir, "equity_state.json")
    if os.path.exists(eq_path):
        with open(eq_path, "r", encoding="utf-8") as f:
            equity_state = json.load(f)
    day_start_eq = equity_state.get("day_start", {}).get(day_key)
    peak = equity_state.get("peak_micro")

    # RiskBreaker 仅在状态转移/基线推进时落盘；文件缺失 = 从未发生
    # 熔断事件，初始态即 NORMAL（risk_breaker._Persisted 默认值）。
    breaker_state = "NORMAL（无落盘记录，未发生熔断事件）"
    bs_path = os.path.join(run_dir, "risk_breaker_state.json")
    if os.path.exists(bs_path):
        with open(bs_path, "r", encoding="utf-8") as f:
            breaker_state = json.load(f).get("state", "未知")

    pool_changes: List[Dict] = []
    pe_path = os.path.join(run_dir, "pool_events.jsonl")
    if os.path.exists(pe_path):
        with open(pe_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                # 事件 at 为 UTC ISO，换算到交易日（Asia/Shanghai）归属：
                ev_day = _day_key(datetime.fromisoformat(ev["at"])) \
                    if ev.get("at") else None
                if ev_day == day_key:
                    pool_changes.append(ev)

    def usd(micro_val: Optional[int]) -> str:
        return "—" if micro_val is None else f"{micro_to_decimal(micro_val)} USDC"

    lines = [
        f"# 纸面日报 {day_key}（闸门 A 纸面同构）",
        "",
        f"- 账本现金余额：{usd(cash)}（口径：cash_ledger 累计，含种子金与成交结算）",
        f"- 当日日初净值锚：{usd(day_start_eq)}（equity_state 记录的当日首见净值，含库存 MtM）",
        f"- 历史净值峰值：{usd(peak)}",
        f"- 当日成交笔数：{len(fills)}（累计 {total_fills}）",
        f"- 当日手续费合计：{usd(day_fees)}（纸面 CostModel 口径，默认费率 0，见引擎登记）",
        f"- 当日 markout(5m) 样本数：{len(samples)}，中位数（价格单位）："
        f"{'—' if day_median is None else micro_to_decimal(day_median)}",
        f"- 累计 markout(5m) 样本数：{len(all_samples)}，中位数（价格 micro）：{all_median}",
        f"- 熔断状态（落盘）：{breaker_state}",
        f"- 当日市场池变动：{len(pool_changes)} 条",
    ]
    for ev in pool_changes:
        lines.append(f"  - {ev.get('at')} {ev.get('type')} "
                     f"{json.dumps({k: v for k, v in ev.items() if k not in ('at', 'type')}, ensure_ascii=False)}")
    lines += [
        "",
        "口径注：当日盈亏未单列现金口径数字——纸面净值含库存 MtM，须以"
        "运行期 equity 序列为准；本日报的净值锚/峰值来自 equity_state.json，"
        "成交与 markout 全部取自账本实数，不估算。",
    ]
    os.makedirs(os.path.join(run_dir, "reports"), exist_ok=True)
    path = os.path.join(run_dir, "reports", f"report-{day_key}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_run_dir() -> str:
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(repo_dir, "..", "..", "stage1", "paper_run"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="闸门 A 纸面长跑编排器")
    parser.add_argument("--run-dir", default=_default_run_dir())
    parser.add_argument("--pool-target", type=int, default=DEFAULT_POOL_TARGET)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--rounds", type=int, default=None)
    parser.add_argument("--duration-min", type=float, default=None)
    parser.add_argument("--seed-usd", default="200")
    parser.add_argument("--base-shares", default="20")
    parser.add_argument("--report-only", action="store_true",
                        help="只生成当日日报并退出")
    parser.add_argument("--no-pool-cache", action="store_true",
                        help="禁用选池缓存恢复（加固线 2 登记 6），启动必走全量选池")
    args = parser.parse_args(argv)

    if args.report_only:
        path = daily_report(args.run_dir)
        print(f"日报已生成：{path}")
        return 0

    runner = PaperRunner(
        args.run_dir, pool_target=args.pool_target,
        candidate_limit=args.candidate_limit,
        poll_interval_sec=args.poll_interval,
        seed_usd=args.seed_usd, base_shares=args.base_shares,
        use_pool_cache=not args.no_pool_cache)
    summary = runner.run(
        rounds=args.rounds,
        duration_sec=args.duration_min * 60 if args.duration_min else None)
    path = daily_report(args.run_dir)
    print(f"运行结束：{json.dumps(summary, ensure_ascii=False, default=str)}")
    print(f"日报已生成：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
