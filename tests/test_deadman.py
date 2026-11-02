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
