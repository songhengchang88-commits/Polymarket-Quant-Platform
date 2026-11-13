import pytest
from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory, is_kill_switch_active


def test_kill_switch_enforces_dry_run():
    if is_kill_switch_active():
        eng = Crypto5mEngine(dry_run=False)
        assert eng.dry_run is True


def test_fair_probs_direction_and_time_decay():
    # 现货高于基准 K，UP 概率 > 0.5
    p_up_long, p_dn_long = Crypto5mEngine.compute_fair_probs(
        spot=70100.0, strike_k=70000.0, vol_1m=0.001, rem_sec=240.0
    )
    assert p_up_long > 0.5
    assert abs((p_up_long + p_dn_long) - 1.0) < 1e-6

    # 同等偏离下，剩余时间越短 (30s vs 240s)，确定性越高
    p_up_short, _ = Crypto5mEngine.compute_fair_probs(
        spot=70100.0, strike_k=70000.0, vol_1m=0.001, rem_sec=30.0
    )
    assert p_up_short > p_up_long


def test_anti_toxic_grid_never_bids_above_fair():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    fair_up, fair_dn = 0.60, 0.40
    orders = eng.build_safe_grid(fair_up, fair_dn, inv)
    assert len(orders) > 0
    for od in orders:
        if od.side == "UP":
            assert od.price <= fair_up - 0.015 + 1e-9
        else:
            assert od.price <= fair_dn - 0.015 + 1e-9
        assert od.cost_usd >= 1.00


def test_inventory_cross_time_hedge_cap():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
        up_shares=10.0,
        up_cost=5.5,  # avg_up_cost = 0.55
    )
    orders = eng.build_safe_grid(fair_up=0.45, fair_dn=0.55, inv=inv)
    # 硬锁验证: 已有未配对 UP 持仓时，绝不允许再在 UP 侧挂开仓单
    assert all(o.side == "DOWN" for o in orders)
    hedge_orders = [o for o in orders if o.side == "DOWN" and o.tag == "hedge"]
    assert len(hedge_orders) == 1
    # 0.96 - 0.55 = 0.41 -> 绝不超过 0.41，且份数精确匹配未配对份数 10.0
    assert hedge_orders[0].price <= 0.41 + 1e-9
    assert hedge_orders[0].shares == pytest.approx(10.0)


def test_late_window_blocks_new_single_leg_entries():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    # 剩余 <= 120s 且当前无持仓时，禁止新开单边试探单
    orders = eng.build_safe_grid(fair_up=0.50, fair_dn=0.50, inv=inv, rem_sec=110.0)
    assert orders == []


def test_shadow_fill_updates_inventory():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    inv.open_orders = eng.build_safe_grid(0.50, 0.50, inv)
    # 模拟真实卖一跌穿我们的 discount 档 (0.47)
    eng.simulate_shadow_fills(inv, asks_up=[(0.45, 50.0)], asks_dn=[(0.56, 50.0)])
    assert inv.up_shares > 0
    assert inv.down_shares == 0
    assert eng.total_fills >= 1


def test_late_window_salvage_hedge_cap():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
        up_shares=10.0,
        up_cost=3.5,  # avg_up_cost = 0.35
    )
    # 在极尾盘 50s 且现货深度背离 (fair_up=0.10, fair_dn=0.90) 时，启动减损对冲放宽至 1.12 - 0.35 = 0.77
    orders = eng.build_safe_grid(fair_up=0.10, fair_dn=0.90, inv=inv, rem_sec=50.0)
    assert len(orders) == 1
    assert orders[0].side == "DOWN"
    assert orders[0].tag == "hedge"
    assert orders[0].price == pytest.approx(0.77, abs=0.01)


def test_drift_triggered_escalation():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
        up_shares=10.0,
        up_cost=3.5,  # avg_up_cost = 0.35
    )
    # 前期 (rem_sec=200s)，但现货突发单边下杀 (fair_up=0.30 < 0.35)，触发漂移平价上限 target_cap=1.00 -> 1.00 - 0.35 = 0.65
    orders = eng.build_safe_grid(fair_up=0.30, fair_dn=0.70, inv=inv, rem_sec=200.0)
    assert len(orders) == 1
    assert orders[0].side == "DOWN"
    assert orders[0].tag == "hedge"
    # min(0.70 - 0.015, 1.00 - 0.35) = min(0.685, 0.65) = 0.65
    assert orders[0].price == pytest.approx(0.65, abs=0.01)


def test_macro_storm_blocks_entries():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="eth-updown-5m-1000",
        coin="eth",
        base_ts=1000,
        strike_k=2000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    # 当全市场发生单边暴跌风暴 (macro_storm_active=True) 时，即便处于中枢平衡区也禁止新开仓
    orders = eng.build_safe_grid(fair_up=0.50, fair_dn=0.50, inv=inv, rem_sec=200.0, macro_storm_active=True)
    assert orders == []
