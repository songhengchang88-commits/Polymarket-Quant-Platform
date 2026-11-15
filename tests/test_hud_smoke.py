"""
tests/test_hud_smoke.py
针对当前 18899 端口实战指挥舰 (HUD) 的端到端集成烟测。
"""
import json
import os
import urllib.request
import pytest

_BASE = os.getenv("PMQ_HUD_URL", "http://127.0.0.1:18899").rstrip("/")
_TIMEOUT = float(os.getenv("PMQ_HUD_TIMEOUT", "3.0"))


def test_hud_dashboard_smoke():
    """验证实战指挥舰 /api/dashboard 接口强契约与字段完整性"""
    req = urllib.request.Request(f"{_BASE}/api/dashboard", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        pytest.skip(f"实战指挥舰未启动 ({_BASE}/api/dashboard): {exc}")

    assert isinstance(data, dict), "HUD 响应必须为 JSON 对象"
    assert "live" in data, "必须包含实时状态 live"
    assert "summary" in data, "必须包含汇总总账 summary"
    assert "rounds" in data, "必须包含轮次列表 rounds"
    assert "trades" in data, "必须包含成交明细 trades"
    assert isinstance(data["rounds"], list)
    assert isinstance(data["trades"], list)
