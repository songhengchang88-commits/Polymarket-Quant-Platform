"""订单心跳循环组件（死人开关调度层）。

定位（规格《心跳自研规格.md》§3）：py-sdk 只封装了单次发送
``SecureClient.send_order_heartbeat()``，周期调度、ID 链管理与失败联动
由调用方负责——本模块即该自研循环组件。

设计要点：
- 发送动作经注入的 ``sender`` 函数完成：``sender(current_id) -> next_id``。
  生产接线时由交易 bot 把 SDK 的 ``send_order_heartbeat`` 适配成该签名
  （返回 ``response.heartbeat_id``）；本组件不 import SDK、不读任何凭证。
- ID 不匹配以鸭子类型识别：异常对象带有 ``heartbeat_id`` 属性（str）即
  视为不匹配错误（py-sdk 的 ``OrderHeartbeatMismatchError`` 正是此形态），
  组件按其携带的期望 ID 立即纠正重发并计数告警。被拒发送不刷新截止期，
  纠正必须抢在窗口内完成（规格 §1.4/§3.3）。
- 失败分级联动（以单调时钟距最后一次被接受发送的间隔驱动，规格 §3.4）：
  >= stale_after（默认 7 秒）触发 ``on_stale``（交易侧应停止发出新订单）；
  >= dead_after（默认 10 秒）触发 ``on_dead``（交易侧应本地主动全撤）；
  恢复被接受后触发 ``on_recover``（是否重新开闸由交易侧决定，本组件
  在 dead 触发后会置 ``dead_latched`` 闩锁供交易侧检查，不自动清除）。
- 串行与单例：同一 ``key_id`` 全局只许一个运行中的实例（类级注册表），
  重复启动（含同实例二次 start）直接抛 RuntimeError；发送经锁串行化，
  同一时刻最多一个在途请求。
- 存活绑定：循环跑在独立线程，另设看门狗线程监测循环线程存活与
  存活戳新鲜度；循环线程死亡或卡死即回调 ``on_dead`` 并置 FAILED。

状态机：UNARMED（未启动/已停止）/ ARMED（正常）/ DEGRADED（已触发
on_stale）/ FAILED（已触发 on_dead 或致命错误）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["HeartbeatLoop", "HeartbeatState"]

# 发送方签名：入参为本次应携带的 heartbeat_id，返回下一次应携带的 ID。
SenderFn = Callable[[str], str]
CallbackFn = Callable[[], None]

# 单次尝试内允许的 ID 纠正次数上限，防止服务端异常时无限纠正空转。
_MAX_CORRECTIONS_PER_ATTEMPT = 3


class HeartbeatState:
    UNARMED = "UNARMED"
    ARMED = "ARMED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


def _mask_id(heartbeat_id: str) -> str:
    """日志脱敏：只记 ID 的哈希前 8 位，不落明文。"""
    if not heartbeat_id:
        return "<empty>"
    return "sha256:" + hashlib.sha256(heartbeat_id.encode()).hexdigest()[:8]


def _is_mismatch(exc: BaseException) -> Optional[str]:
    """鸭子类型识别 ID 不匹配：异常带 str 类型的 heartbeat_id 属性。"""
    expected = getattr(exc, "heartbeat_id", None)
    if isinstance(expected, str):
        return expected
    return None


def _is_fatal(exc: BaseException) -> bool:
    """致命错误识别：显式 fatal 标记，或鉴权类 HTTP 状态（401/403）。

    鉴权失败下心跳不可能自愈，按规格 §3.3 直接走最高级联动。
    """
    if getattr(exc, "fatal", False):
        return True
    status = getattr(exc, "status", None)
    return status in (401, 403)


class HeartbeatLoop:
    """心跳循环组件，实现 sdk_adapter.HeartbeatPort 的 start/stop/send_once。

    参数：
        sender: 注入的单次发送函数 ``sender(current_id) -> next_id``。
        key_id: 该循环绑定的 API key 标识（仅作单例注册键，不涉及凭证本身）。
        interval: 发送节拍秒数，默认 5.0（规格 §1.2 证实值）。
        stale_after: 距最后被接受发送多久触发 on_stale，默认 7.0。
        dead_after: 距最后被接受发送多久触发 on_dead，默认 10.0。
        retry_delay: 单次发送失败后的重试间隔，默认 1.0（规格 §3.3）。
        on_stale / on_dead / on_recover: 联动回调，均可为空。
        cancel_all: 正常停机时先行执行的全撤回调（stop(cancel_first=True)
            时调用），由交易侧注入；为空则跳过撤单步骤。
        clock: 单调时钟函数，默认 time.monotonic（测试可注入）。
        watchdog_timeout: 循环线程存活戳超过该秒数未更新视为卡死，
            默认 max(2*interval, 1.0)。
    """

    _registry: Dict[str, "HeartbeatLoop"] = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        sender: SenderFn,
        *,
        key_id: str = "default",
        interval: float = 5.0,
        stale_after: float = 7.0,
        dead_after: float = 10.0,
        retry_delay: float = 1.0,
        on_stale: Optional[CallbackFn] = None,
        on_dead: Optional[CallbackFn] = None,
        on_recover: Optional[CallbackFn] = None,
        cancel_all: Optional[CallbackFn] = None,
        clock: Callable[[], float] = time.monotonic,
        watchdog_timeout: Optional[float] = None,
    ) -> None:
        if not callable(sender):
            raise TypeError("sender 必须是可调用对象：sender(current_id) -> next_id")
        if interval <= 0:
            raise ValueError("interval 必须为正数")
        if not (0 < stale_after < dead_after):
            raise ValueError("阈值须满足 0 < stale_after < dead_after")
        self._sender = sender
        self._key_id = key_id
        self._interval = float(interval)
        self._stale_after = float(stale_after)
        self._dead_after = float(dead_after)
        self._retry_delay = float(retry_delay)
        self._on_stale = on_stale
        self._on_dead = on_dead
        self._on_recover = on_recover
        self._cancel_all = cancel_all
        self._clock = clock
        self._watchdog_timeout = (
            float(watchdog_timeout)
            if watchdog_timeout is not None
            else max(2.0 * self._interval, 1.0)
        )
        # 看门狗与主循环的唤醒粒度：足够及时评估 7/10 秒阈值，又不空转烧 CPU。
        self._poll = min(0.25, self._interval / 10.0)

        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._current_id: str = ""
        self._state = HeartbeatState.UNARMED
        self._last_accepted_at: Optional[float] = None
        self._started_at: Optional[float] = None
        self._stale_fired = False
        self._dead_fired = False
        # dead 闩锁：on_dead 触发后保持置位，须交易侧确认后调 reset_latch()。
        self.dead_latched = False

        # 可观测计数（规格 §3.5 的最小集）。
        self.send_count = 0
        self.accepted_count = 0
        self.failure_count = 0
        self.mismatch_count = 0

        self._send_stop = threading.Event()
        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._loop_alive_at: Optional[float] = None
        self._running = False

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def current_id(self) -> str:
        return self._current_id

    @property
    def last_accepted_at(self) -> Optional[float]:
        return self._last_accepted_at

    @property
    def is_running(self) -> bool:
        return self._running

    def seconds_since_accepted(self) -> Optional[float]:
        """距最后一次被接受发送的秒数；从未被接受时以启动时刻为基准。

        尚未启动返回 None。基准口径见交付说明偏离登记 D2。
        """
        baseline = self._last_accepted_at
        if baseline is None:
            baseline = self._started_at
        if baseline is None:
            return None
        return self._clock() - baseline

    # ------------------------------------------------------------------
    # HeartbeatPort 接口
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动循环线程与看门狗线程。重复启动（同实例或同 key_id）即拒。"""
        with HeartbeatLoop._registry_lock:
            if self._running:
                raise RuntimeError("HeartbeatLoop 已在运行，拒绝重复启动")
            existing = HeartbeatLoop._registry.get(self._key_id)
            if existing is not None and existing is not self and existing.is_running:
                raise RuntimeError(
                    f"key_id={self._key_id!r} 已有心跳循环在运行，拒绝双开"
                    "（双实例并发发送会互相制造 ID 不匹配）"
                )
            HeartbeatLoop._registry[self._key_id] = self

        self._send_stop.clear()
        self._shutdown.clear()
        self._started_at = self._clock()
        self._loop_alive_at = self._started_at
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"heartbeat-loop-{self._key_id}",
            daemon=True,
        )
        self._watchdog_thread = threading.Thread(
            target=self._run_watchdog,
            name=f"heartbeat-watchdog-{self._key_id}",
            daemon=True,
        )
        self._thread.start()
        self._watchdog_thread.start()
        logger.info(
            "心跳循环启动 key=%s interval=%.2fs stale=%.1fs dead=%.1fs",
            self._key_id,
            self._interval,
            self._stale_after,
            self._dead_after,
        )

    def stop(self, *, cancel_first: bool = True) -> None:
        """正常关停：先（可选）全撤，再停发，最后回收线程与注册。

        依据规格 §3.4 关停序列与 §1.3：停发后注册在过期前依然有效，
        只要挂单已先行清空即安全。未启动时调用为无操作。
        """
        if not self._running:
            return
        if cancel_first and self._cancel_all is not None:
            try:
                self._cancel_all()
            except Exception:
                logger.exception("停机全撤回调抛错，继续停发心跳")
        self._send_stop.set()
        self._shutdown.set()
        for thread in (self._thread, self._watchdog_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(2.0, 2.0 * self._interval))
        self._running = False
        with HeartbeatLoop._registry_lock:
            if HeartbeatLoop._registry.get(self._key_id) is self:
                del HeartbeatLoop._registry[self._key_id]
        with self._state_lock:
            if self._state != HeartbeatState.FAILED:
                self._state = HeartbeatState.UNARMED
        logger.info("心跳循环已停止 key=%s", self._key_id)

    def send_once(self) -> str:
        """串行发送一次（含 ID 不匹配即时纠正），返回下一轮应携带的 ID。

        成功时更新 ID 链与被接受时刻；失败时计数后原样抛出异常，
        联动阈值评估由循环线程负责（手动调用方可自行读
        seconds_since_accepted() 决策）。
        """
        with self._send_lock:
            return self._attempt_send()

    def reset_latch(self) -> None:
        """交易侧完成对账/人工确认后清除 dead 闩锁（不改变联动触发标志，
        触发标志随下一次被接受发送自动复位）。"""
        self.dead_latched = False

    # ------------------------------------------------------------------
    # 发送与联动（内部）
    # ------------------------------------------------------------------

    def _attempt_send(self) -> str:
        """执行一次发送尝试；不匹配时按期望 ID 立即纠正重发（有限次）。"""
        attempt_id = self._current_id
        corrections = 0
        while True:
            self.send_count += 1
            try:
                next_id = self._sender(attempt_id)
            except Exception as exc:
                expected = _is_mismatch(exc)
                if expected is not None:
                    self.mismatch_count += 1
                    corrections += 1
                    logger.warning(
                        "心跳 ID 不匹配（第 %d 次），按期望 ID 纠正重发 key=%s sent=%s expected=%s",
                        self.mismatch_count,
                        self._key_id,
                        _mask_id(attempt_id),
                        _mask_id(expected),
                    )
                    if corrections > _MAX_CORRECTIONS_PER_ATTEMPT:
                        self.failure_count += 1
                        raise
                    # 纠正后的 ID 先落链：即便重发再失败，下次也以服务端
                    # 期望 ID 为准收敛（规格 §3.3 / L1-P3）。
                    self._current_id = expected
                    attempt_id = expected
                    continue
                self.failure_count += 1
                if _is_fatal(exc):
                    logger.error("心跳致命错误（鉴权类），直接触发最高级联动：%r", exc)
                    self._fire_dead(reason="fatal")
                raise
            if not isinstance(next_id, str):
                self.failure_count += 1
                raise TypeError(
                    f"sender 必须返回 str 类型的下一 heartbeat_id，实际 {type(next_id)!r}"
                )
            self._current_id = next_id
            self._last_accepted_at = self._clock()
            self.accepted_count += 1
            self._on_accepted()
            logger.debug(
                "心跳被接受 key=%s next=%s", self._key_id, _mask_id(next_id)
            )
            return next_id

    def _on_accepted(self) -> None:
        """一次发送被接受后的状态复位与恢复回调。"""
        was_degraded = self._stale_fired or self._dead_fired
        self._stale_fired = False
        self._dead_fired = False
        with self._state_lock:
            self._state = HeartbeatState.ARMED
        if was_degraded:
            logger.info("心跳恢复被接受 key=%s", self._key_id)
            self._safe_callback(self._on_recover, "on_recover")

    def _evaluate_linkage(self) -> None:
        """按距最后被接受发送的间隔评估分级联动（每级每轮故障只触发一次）。"""
        elapsed = self.seconds_since_accepted()
        if elapsed is None:
            return
        if elapsed >= self._dead_after and not self._dead_fired:
            self._fire_dead(reason=f"no_accepted_for_{elapsed:.1f}s")
        elif elapsed >= self._stale_after and not self._stale_fired:
            self._stale_fired = True
            with self._state_lock:
                if self._state == HeartbeatState.ARMED or self._state == HeartbeatState.UNARMED:
                    self._state = HeartbeatState.DEGRADED
            logger.warning(
                "心跳 stale：距最后被接受 %.1f 秒（阈值 %.1f），应停止新订单 key=%s",
                elapsed,
                self._stale_after,
                self._key_id,
            )
            self._safe_callback(self._on_stale, "on_stale")

    def _fire_dead(self, *, reason: str) -> None:
        self._dead_fired = True
        self.dead_latched = True
        with self._state_lock:
            self._state = HeartbeatState.FAILED
        logger.error("心跳 dead（%s）：交易侧应本地主动全撤 key=%s", reason, self._key_id)
        self._safe_callback(self._on_dead, "on_dead")

    @staticmethod
    def _safe_callback(callback: Optional[CallbackFn], name: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            logger.exception("心跳联动回调 %s 抛错（已吞）", name)

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        next_send_at = self._clock()
        try:
            while not self._send_stop.is_set():
                self._loop_alive_at = self._clock()
                now = self._clock()
                if now >= next_send_at:
                    try:
                        # 不与手动 send_once 并发：拿不到锁说明在途，跳过本拍。
                        if self._send_lock.acquire(blocking=False):
                            try:
                                self._attempt_send()
                            finally:
                                self._send_lock.release()
                        next_send_at = self._clock() + self._interval
                    except Exception:
                        # 失败不忙等：按 retry_delay 退避后重试（规格 §3.3）。
                        next_send_at = self._clock() + min(
                            self._retry_delay, self._interval
                        )
                self._evaluate_linkage()
                self._send_stop.wait(self._poll)
        except Exception:
            logger.exception("心跳循环线程异常退出 key=%s", self._key_id)
            # 线程死亡由看门狗检出并触发 on_dead；此处只记录。

    def _run_watchdog(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(self._poll)
            if self._shutdown.is_set() or not self._running:
                return
            thread_dead = self._thread is not None and not self._thread.is_alive()
            alive_at = self._loop_alive_at
            thread_stuck = (
                alive_at is not None
                and (self._clock() - alive_at) > self._watchdog_timeout
            )
            if thread_dead or thread_stuck:
                if not self._dead_fired:
                    reason = "loop_thread_dead" if thread_dead else "loop_thread_stuck"
                    logger.error("看门狗检出心跳循环异常（%s）key=%s", reason, self._key_id)
                    self._fire_dead(reason=reason)
                # 已上报即退出看门狗，避免重复评估；交易侧应走整体关停。
                return
