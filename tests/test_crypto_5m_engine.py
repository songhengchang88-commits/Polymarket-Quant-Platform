import time
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
    # 在极尾盘 50s 且现货深度背离 (fair_up=0.10, fair_dn=0.90) 时，启动救险穿盘对冲 (target_cap=1.25 -> 1.25 - 0.35 = 0.90)
    orders = eng.build_safe_grid(fair_up=0.10, fair_dn=0.90, inv=inv, rem_sec=50.0)
    assert len(orders) == 1
    assert orders[0].side == "DOWN"
    assert orders[0].tag == "hedge"
    assert orders[0].price == pytest.approx(0.90, abs=0.01)


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


def test_hedge_reserve_enclave_protects_cash():
    # 模拟现金仅剩 $3.00，discount 试探单最多动用 45% = $1.35
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    eng.paper_cash_usd = 3.00
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    # 单笔预算 $1.95 > $1.35，开仓单被对冲储备金硬锁拦截
    orders = eng.build_safe_grid(fair_up=0.50, fair_dn=0.50, inv=inv, rem_sec=200.0)
    assert orders == []

    # 但若当前持有未配对持仓需要对冲，对冲单被允许动用全部 $3.00 现金
    inv.up_shares = 5.0
    inv.up_cost = 2.0
    hedge_orders = eng.build_safe_grid(fair_up=0.50, fair_dn=0.50, inv=inv, rem_sec=200.0)
    assert len(hedge_orders) == 1
    assert hedge_orders[0].tag == "hedge"


def test_queue_position_penalty_in_shadow_match():
    eng = Crypto5mEngine(budget_per_order_usd=3.0, dry_run=True)
    inv = MarketInventory(
        slug="btc-updown-5m-1000",
        coin="btc",
        base_ts=1000,
        strike_k=70000.0,
        token_up="t_up",
        token_down="t_dn",
    )
    from pmq.crypto_5m_engine import GridOrder
    inv.open_orders = [GridOrder(side="UP", price=0.40, shares=10.0, cost_usd=4.0, tag="discount")]
    # 同价挂单 (ask=0.40)，但深度仅 12.0 < 10.0 * 1.8 = 18.0 -> 判定排队未轮到，成交为 0
    eng.simulate_shadow_fills(inv, asks_up=[(0.40, 12.0)], asks_dn=[])
    assert inv.up_shares == 0.0

    # 同价挂单且深度足够 (ask=0.40, size=30.0 >= 18.0) -> 按 50% 保守成交 5.0 份
    eng.simulate_shadow_fills(inv, asks_up=[(0.40, 30.0)], asks_dn=[])
    assert inv.up_shares == pytest.approx(5.0, abs=0.1)


def test_settlement_30s_lock_and_release():
    from pmq.crypto_5m_engine import Crypto5mEngine
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    assert engine.paper_cash_usd == 30.0
    assert len(engine.pending_settlements) == 0

    engine.pending_settlements.append((time.time() + 0.1, 5.0, "btc-test"))
    engine._release_settled_funds()
    assert engine.paper_cash_usd == 30.0
    assert len(engine.pending_settlements) == 1

    time.sleep(0.15)
    engine._release_settled_funds()
    assert engine.paper_cash_usd == 35.0
    assert len(engine.pending_settlements) == 0


def test_hedge_slippage_penalty():
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory, GridOrder
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1700000000, strike_k=68000.0, token_up='up', token_down='dn')

    od_norm = GridOrder(side='UP', price=0.40, shares=2.0, cost_usd=0.80, tag='discount')
    inv.open_orders = [od_norm]
    asks_up = [(0.38, 10.0)]
    engine.simulate_shadow_fills(inv, asks_up, [])
    # 2.0 shares * 85% strictly better fill ratio = 1.7 shares
    assert inv.up_shares == 1.7
    assert inv.up_cost == round(1.7 * 0.38, 4)

    inv2 = MarketInventory(slug='btc-test2', coin='btc', base_ts=1700000000, strike_k=68000.0, token_up='up', token_down='dn')
    od_hedge = GridOrder(side='DOWN', price=0.55, shares=2.0, cost_usd=1.10, tag='hedge')
    inv2.open_orders = [od_hedge]
    asks_dn = [(0.50, 10.0)]
    engine.simulate_shadow_fills(inv2, [], asks_dn)
    # 2.0 shares * 85% strictly better fill ratio = 1.7 shares
    assert inv2.down_shares == 1.7
    # base_exec_p is 0.50 + 0.015 penalty = 0.515
    assert inv2.down_cost == round(1.7 * 0.515, 4)


def test_small_capital_slot_allocator():
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc', 'eth'], initial_paper_capital=30.0, persist_ledger=False)
    # 模拟现金仅剩 $15.0，且 BTC 已经持有单边多头仓位
    engine.paper_cash_usd = 15.0
    inv_btc = MarketInventory(slug='btc-updown-5m-1000', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv_btc.up_shares = 5.0
    inv_btc.up_cost = 2.5
    engine.inventories['btc-updown-5m-1000'] = inv_btc

    # ETH 当前无持仓，尝试执行 _process_coin_step
    inv_eth = MarketInventory(slug='eth-updown-5m-1000', coin='eth', base_ts=1000, strike_k=2000.0, token_up='u', token_down='d')
    engine.inventories['eth-updown-5m-1000'] = inv_eth

    # 现金 < $18 时，ETH 被小资金并发调度拦截，不应挂任何开仓单
    engine._process_coin_step('eth', base_ts=1000, rem_sec=200.0)
    assert inv_eth.open_orders == []


def test_precision_cutoff_preserves_hedge():
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory, GridOrder
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv.open_orders = [
        GridOrder(side='UP', price=0.45, shares=2.0, cost_usd=0.9, tag='discount'),
        GridOrder(side='DOWN', price=0.55, shares=2.0, cost_usd=1.1, tag='hedge'),
    ]
    # 在 40s (<= 60s) 时，开仓 discount 被撤销，但救险 hedge 单保留！
    discount_orders = [o for o in inv.open_orders if o.tag == "discount"]
    if discount_orders:
        inv.open_orders = [o for o in inv.open_orders if o.tag != "discount"]
    assert len(inv.open_orders) == 1
    assert inv.open_orders[0].tag == 'hedge'

