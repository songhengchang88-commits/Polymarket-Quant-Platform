"""死人开关与心跳卫士 (Dead-Man Switch & Heartbeat Guard).

这是保卫本金的最底层物理熔断栓:
1. 注册 Python atexit 终结钩子与系统信号捕获 (SIGINT, SIGTERM, SIGBREAK);
2. 任何异常退出、Ctrl+C、被任务管理器杀死或代码崩溃瞬间, 立即触发同步紧急撤单;
3. 提供后台独立心跳看门狗 (Heartbeat Watchdog), 主循环每次健康采样时喂狗;
4. 一旦超过指定阈值未收到主循环心跳 (如主循环卡死/网络阻塞), 卫士强制触发 cancel_all() 并拉响警报;
5. 所有动作写入持久化安全日志, 确保退出时"订单簿内零残留挂单", 杜绝对手盘趁虚打穿本金。

两种停机语义 (重要):
- **异常停机** (崩溃 / 被 kill / 看门狗超时): 必须紧急撤单 —— 进程可能再也不会回来。
- **优雅停机** (调用方显式 stop() / 正常退出): 只做"先撤单、再停发", 绝不误报"死人"。

旧实现把两者混为一谈: atexit 一律按 PROCESS_ATEXIT 紧急撤单, 于是**任何一次正常
stop() 都会被当成进程死亡**, 日志里刷 "存活端口监听检测失败! 退出原因: PROCESS_ATEXIT"
这种误导性告警。现在 stop()/disarm() 会明确标记"已知会停", atexit 与信号处理器据此
区分异常与优雅, 误报消失, 而真正的崩溃路径依旧拉响警报。
"""
from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("pmq.deadman")


class DeadManSwitch:
    """死人开关: 守护进程生存周期, 确保离线即撤单。"""

    # 全进程所有实例登记在册: 优雅停机时统一撤单, 避免漏掉某个入口实例
    _registry: List["DeadManSwitch"] = []
    _registry_lock = threading.Lock()

    def __init__(
        self,
        cancel_callback: Callable[[], Any],
        heartbeat_timeout_sec: float = 30.0,
        name: str = "DeadManSwitch",
    ):
        """
        :param cancel_callback: 执行全额撤单的可调用对象 (例如 client.cancel_all)
        :param heartbeat_timeout_sec: 看门狗最大允许失联秒数 (默认 30 秒)
        :param name: 实例名称标识
        """
        self.cancel_callback = cancel_callback
        self.timeout = max(0.1, float(heartbeat_timeout_sec))
        self.name = name

        self._last_heartbeat = time.time()
        self._is_active = False
        self._triggered = False
        self._disarmed = False          # 已知会正常停, atexit 不再误报"进程死亡"
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None

        with DeadManSwitch._registry_lock:
            DeadManSwitch._registry.append(self)

        # 注册退出钩子
        self._register_exit_hooks()

    # ---------------- 核心触发与安全撤单 ----------------
    def trigger_emergency_cancel(self, reason: str = "MANUAL_TRIGGER") -> Dict[str, Any]:
        """立即执行紧急全额撤单, 并保证幂等 (只执行一次)。"""
        with self._lock:
            if self._triggered:
                return {"status": "ALREADY_TRIGGERED", "reason": reason}
            self._triggered = True

        logger.critical(f"🚨 [{self.name}] 触发死人开关紧急撤单! 触发原因: {reason}")
        # 已触发即作废, 从全局注册表摘除: 注册表只增不减会在长期运行中累积失效实例
        self._registry_discard()
        res_info = {
            "status": "TRIGGERED",
            "reason": reason,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": False,
            "error": None,
        }

        try:
            if callable(self.cancel_callback):
                ret = self.cancel_callback()
                res_info["success"] = True
                res_info["result"] = str(ret)
                logger.info(f"🛡️ [{self.name}] 紧急撤单已执行完成，回执: {ret}")
            else:
                res_info["error"] = "cancel_callback is not callable"
                logger.error(f"❌ [{self.name}] cancel_callback 不可调用")
        except Exception as e:
            res_info["error"] = str(e)
            logger.exception(f"❌ [{self.name}] 紧急撤单调用异常: {e}")

        return res_info

    # ---------------- 优雅停机: 只撤单不拉警报 ----------------
    def stop(self, *, cancel_first: bool = True) -> None:
        """优雅停机: 先撤单, 再停看门狗, 并标记为"已知会停"。

        与 trigger_emergency_cancel 的唯一区别是**语义与告警级别**, 不是要不要撤单:
        优雅停机同样必须把订单撤干净(不得裸奔停服), 但它不是"死人"事故,
        因此不写 CRITICAL 告警、不置 _triggered 事故位、不会在 atexit 里重复误报。

        撤单本身失败意味着订单簿可能残留挂单 -> 自动升级为紧急路径, 让事故可见。
        """
        self.disarm("GRACEFUL_STOP")
        self.stop_watchdog()
        if not cancel_first:
            return
        if not callable(self.cancel_callback):
            return
        try:
            ret = self.cancel_callback()
            logger.info(f"🛡️ [{self.name}] 优雅停机撤单完成，回执: {ret}")
        except Exception as e:
            logger.error(f"❌ [{self.name}] 优雅停机撤单异常, 升级为紧急路径: {e}")
            self.trigger_emergency_cancel(reason="GRACEFUL_CANCEL_FAILED")

    def disarm(self, reason: str = "") -> None:
        """声明"本进程已知会正常停", 屏蔽 atexit 的紧急撤单误报。

        同时从全局注册表摘除自身 —— 优雅停过的开关已无驻留必要, 否则注册表会
        只增不减, 每次停机都重复触碰早已作废的实例。
        """
        with self._lock:
            self._disarmed = True
        self._registry_discard()
        if reason:
            logger.info(f"ℹ️ [{self.name}] 已声明优雅停机 ({reason}), atexit 不再按进程死亡处理")

    def _registry_discard(self) -> None:
        """把自身从全局注册表摘除 (幂等)。"""
        with DeadManSwitch._registry_lock:
            if self in DeadManSwitch._registry:
                DeadManSwitch._registry.remove(self)

    @property
    def disarmed(self) -> bool:
        return self._disarmed

    # ---------------- 钩子与信号捕获 ----------------
    def _register_exit_hooks(self):
        # 1. 注册 atexit 标准库退出处理
        atexit.register(self._on_process_exit)

        # 2. 捕获系统信号
        signals_to_catch = [signal.SIGINT, signal.SIGTERM]
        # Windows 支持 SIGBREAK
        if hasattr(signal, "SIGBREAK"):
            signals_to_catch.append(signal.SIGBREAK)

        for sig in signals_to_catch:
            try:
                original_handler = signal.getsignal(sig)

                def make_handler(s=sig, orig=original_handler):
                    def _sig_handler(signum, frame):
                        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
                        logger.warning(f"⚠️ [{self.name}] 捕获到系统退出信号: {sig_name}")
                        self.trigger_emergency_cancel(reason=f"SIGNAL_{sig_name}")
                        if callable(orig) and orig not in (signal.SIG_IGN, signal.SIG_DFL):
                            orig(signum, frame)
                        else:
                            sys.exit(128 + signum)
                    return _sig_handler

                signal.signal(sig, make_handler())
            except Exception as e:
                logger.debug(f"注册信号 {sig} 捕获轻微提示: {e}")

    def _on_process_exit(self):
        """Python 解释器正常或异常终结时的最后防线。

        已 disarm() 的实例是"已知会正常停", 走优雅撤单而不是紧急撤单;
        未 disarm 的实例才是真的"进程死了没人管"。
        """
        with self._lock:
            disarmed = self._disarmed
        if not self._triggered:
            if disarmed:
                logger.info(f"ℹ️ [{self.name}] 解释器退出 (已声明优雅停机), 跳过紧急撤单")
                return
            self.trigger_emergency_cancel(reason="PROCESS_ATEXIT")

    # ---------------- 看门狗心跳机制 ----------------
    def heartbeat(self):
        """主业务循环每次健康采样时喂狗。"""
        with self._lock:
            self._last_heartbeat = time.time()

    def start_watchdog(self):
        """启动后台看门狗守护线程。"""
        with self._lock:
            if self._is_active:
                return
            self._is_active = True
            self._last_heartbeat = time.time()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name=f"{self.name}_Watchdog"
            )
            self._watchdog_thread.start()
            logger.info(f"🐕 [{self.name}] 心跳看门狗已启动, 超时阈值: {self.timeout}s")

    def stop_watchdog(self):
        """安全停止看门狗。"""
        with self._lock:
            self._is_active = False

    def _watchdog_loop(self):
        """看门狗独立循环检测。"""
        check_interval = min(0.1, max(0.01, self.timeout / 5.0))
        while self._is_active and not self._triggered:
            time.sleep(check_interval)
            now = time.time()
            elapsed = now - self._last_heartbeat
            if elapsed > self.timeout:
                logger.critical(
                    f"💀 [{self.name}] 主进程已失联 {elapsed:.1f}s > {self.timeout}s! 心跳超时, 强制拉响警报并撤单!"
                )
                self.trigger_emergency_cancel(reason=f"HEARTBEAT_TIMEOUT_{elapsed:.1f}s")
                break

    # ---------------- 全局统一停机 ----------------
    @classmethod
    def shutdown_all(cls, *, graceful: bool = True) -> Dict[str, int]:
        """对全进程所有已注册的死人开关统一停机。

        实盘入口只需在 finally 里调一次, 即可保证"任一实例都不会在停机时漏撤单"。
        graceful=True (默认): 全部走优雅撤单, 不产生"死人"误报;
        graceful=False: 全部按异常处理, 拉响紧急撤单。

        注意: 先在锁内快照、再释放锁后逐个停机 —— 若持锁调用 stop()/disarm()
        会与它们内部的注册表加锁形成非重入死锁。
        """
        with cls._registry_lock:
            instances = list(cls._registry)
        triggered = 0
        for inst in instances:
            if graceful:
                inst.stop()
            else:
                inst.trigger_emergency_cancel(reason="GLOBAL_SHUTDOWN")
                triggered += 1
        with cls._registry_lock:
            remaining = len(cls._registry)
        logger.info(f"🧹 全局停机完成: 停机 {len(instances)} 个, 其中 {triggered} 个按紧急路径处理, 注册表残留 {remaining} 个")
        return {"total": len(instances), "emergency": triggered}

    @classmethod
    def active_instances(cls) -> List["DeadManSwitch"]:
        with cls._registry_lock:
            return [i for i in cls._registry if not i._triggered]


def wire_deadman(
    cancel_callback: Callable[[], Any],
    *,
    heartbeat_timeout_sec: float = 30.0,
    name: str = "DeadManSwitch",
) -> DeadManSwitch:
    """工厂: 创建并注册一个死人开关, 供实盘入口直接使用。

    用法:
        dm = wire_deadman(client.cancel_all, heartbeat_timeout_sec=30.0, name="live")
        dm.start_watchdog()
        try:
            run_trading_loop()
        finally:
            DeadManSwitch.shutdown_all()   # 或 dm.stop()
    """
    return DeadManSwitch(
        cancel_callback=cancel_callback,
        heartbeat_timeout_sec=heartbeat_timeout_sec,
        name=name,
    )
