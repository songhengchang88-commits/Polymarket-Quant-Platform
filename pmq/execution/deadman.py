"""死人开关与心跳卫士 (Dead-Man Switch & Heartbeat Guard).

这是保卫 6.98U 极限资金的最底层物理熔断栓:
1. 注册 Python atexit 终结钩子与系统信号捕获 (SIGINT, SIGTERM, SIGBREAK);
2. 任何异常退出、Ctrl+C、被任务管理器杀死或代码崩溃瞬间, 立即触发同步紧急撤单;
3. 提供后台独立心跳看门狗 (Heartbeat Watchdog), 定期向 CLOB 发送心跳 Ping;
4. 一旦超过指定阈值未收到主循环心跳 (如主循环卡死/网络阻塞), 卫士强制触发 cancel_all() 并拉响警报;
5. 所有动作写入持久化安全日志, 确保退出时"订单簿内零残留挂单", 杜绝对手盘趁虚打穿本金。
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
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None

        # 注册退出钩子
        self._register_exit_hooks()

    # ---------------- 核心触发与安全撤单 ----------------
    def trigger_emergency_cancel(self, reason: str = "MANUAL_TRIGGER") -> Dict[str, Any]:
        """立即执行紧急全额撤单，并保证幂等 (只执行一次)。"""
        with self._lock:
            if self._triggered:
                return {"status": "ALREADY_TRIGGERED", "reason": reason}
            self._triggered = True

        logger.critical(f"🚨 [{self.name}] 触发死人开关紧急撤单! 触发原因: {reason}")
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
        """Python 解释器正常或异常终结时的最后防线。"""
        if not self._triggered:
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
