import time
from pmq.execution.deadman import DeadManSwitch


def test_deadman_manual_trigger():
    called = []

    def mock_cancel():
        called.append("cancelled")
        return {"cancelled": True}

    dm = DeadManSwitch(cancel_callback=mock_cancel, heartbeat_timeout_sec=10.0, name="TestSwitch")
    assert not dm._triggered

    res = dm.trigger_emergency_cancel(reason="TEST")
    assert res["status"] == "TRIGGERED"
    assert res["success"] is True
    assert called == ["cancelled"]

    # 再次触发应幂等
    res2 = dm.trigger_emergency_cancel(reason="TEST_AGAIN")
    assert res2["status"] == "ALREADY_TRIGGERED"
    assert len(called) == 1


def test_deadman_watchdog_timeout():
    called = []

    def mock_cancel():
        called.append("timeout_cancelled")
        return "ok"

    # 设置极短超时 0.5s
    dm = DeadManSwitch(cancel_callback=mock_cancel, heartbeat_timeout_sec=0.5, name="WatchdogTest")
    dm.start_watchdog()

    # 等待超过 0.5s，触发看门狗
    time.sleep(1.0)
    assert dm._triggered is True
    assert called == ["timeout_cancelled"]

    dm.stop_watchdog()


def test_deadman_watchdog_heartbeat_keeps_alive():
    called = []

    def mock_cancel():
        called.append("timeout_cancelled")

    dm = DeadManSwitch(cancel_callback=mock_cancel, heartbeat_timeout_sec=1.0, name="AliveTest")
    dm.start_watchdog()

    # 连续喂狗两次，不应触发超时
    for _ in range(3):
        time.sleep(0.3)
        dm.heartbeat()

    assert dm._triggered is False
    assert len(called) == 0

    dm.stop_watchdog()


def test_deadman_graceful_stop_does_not_false_alarm():
    """优雅停机不得被当成"进程死亡": stop()/disarm() 后 atexit 不得紧急撤单。

    旧实现任何一次正常退出都报 PROCESS_ATEXIT 紧急撤单, 日志刷误导性告警,
    且在实盘里会把"计划内停机"伪装成"死人", 掩盖真正的异常。
    """
    called = []

    def mock_cancel():
        called.append("cancelled")
        return {"cancelled": True}

    dm = DeadManSwitch(cancel_callback=mock_cancel, heartbeat_timeout_sec=10.0, name="Graceful")
    dm.start_watchdog()
    dm.stop()                      # 优雅停机: 必须撤单, 但不得拉"死人"警报
    assert dm.disarmed is True

    # 优雅停机同样要撤干净订单(不得裸奔停服)
    assert called == ["cancelled"]
    # 但绝不置事故位 —— 即不会误导 atexit 把计划内停机当成进程死亡
    assert dm._triggered is False

    # 停机后手动触发仍可用 (真实异常路径不受影响)
    res = dm.trigger_emergency_cancel(reason="MANUAL_AFTER_STOP")
    assert res["status"] == "TRIGGERED"
    assert called == ["cancelled", "cancelled"]


def test_deadman_shutdown_all_graceful():
    """全局统一停机: 多个实例一并优雅停, 不产生紧急撤单, 且注册表随之收缩。"""
    called = []

    def mk():
        def cb():
            called.append("x")
            return "ok"
        return cb

    dms = [DeadManSwitch(cancel_callback=mk(), heartbeat_timeout_sec=10.0, name=f"multi{i}")
           for i in range(3)]
    before = len(DeadManSwitch._registry)
    summary = DeadManSwitch.shutdown_all(graceful=True)
    assert summary["emergency"] == 0          # 优雅路径绝不算紧急
    assert summary["total"] >= 3
    assert len(called) >= 3                   # 每个实例都撤了单, 不留挂单
    assert all(d.disarmed for d in dms)
    assert all(d._triggered is False for d in dms)   # 无"死人"事故误报
    # 作废实例被摘除, 注册表不得无界增长
    assert len(DeadManSwitch._registry) <= before


def test_deadman_shutdown_all_emergency_still_works():
    """非优雅全局停机必须照常紧急撤单 (崩溃路径不能被优雅语义削弱)。"""
    called = []

    def mk():
        def cb():
            called.append("y")
            return "ok"
        return cb

    dm = DeadManSwitch(cancel_callback=mk(), heartbeat_timeout_sec=10.0, name="emg")
    summary = DeadManSwitch.shutdown_all(graceful=False)
    assert summary["emergency"] >= 1
    assert called == ["y"]
    assert dm._triggered is True


def test_wire_deadman_factory():
    from pmq.execution.deadman import wire_deadman
    called = []
    dm = wire_deadman(lambda: called.append(1), heartbeat_timeout_sec=5.0, name="factory")
    assert dm.name == "factory"
    assert dm in DeadManSwitch.active_instances()
    dm.stop()
