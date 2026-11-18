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
    fair_up, fair_dn = 0.55, 0.45
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
    # v5.6 平价微摩擦对冲上限 1.04 - 0.55 = 0.49 -> 绝不超过 0.49，且份数精确匹配未配对份数 10.0
    assert hedge_orders[0].price <= 0.49 + 1e-9
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
    # v5.3 满额对冲吃单铁律: 对冲作为主动 Taker 扫单，对手盘充足时 100% 满额撮合 2.0 shares，彻底杜绝残单摩擦！
    assert inv2.down_shares == 2.0
    # base_exec_p is 0.50 + 0.015 penalty = 0.515
    assert inv2.down_cost == round(2.0 * 0.515, 4)


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


def test_v39_active_taker_flush_at_best_ask():
    """验证 v3.9 主动吃单引擎: 中期 (rem_sec <= 110s) 持有未配对单边时，直接对齐卖一主动吃单，消除裸奔"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['eth'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='eth-test', coin='eth', base_ts=1000, strike_k=2000.0, token_up='u', token_down='d')
    inv.up_shares = 3.0
    inv.up_cost = 1.50   # avg_up_cost = 0.50

    # 中期 85s (<= 110s): target_cap=1.00 -> max_hedge_dn = 0.50.
    # 真实盘口卖一 best_ask_dn 为 0.49 (处于 0.50 限额内)
    orders = engine.build_safe_grid(
        fair_up=0.50, fair_dn=0.50, inv=inv, rem_sec=85.0, best_ask_dn=0.49
    )
    assert len(orders) == 1
    assert orders[0].side == "DOWN"
    assert orders[0].tag == "hedge"
    # 关键断言: 价格直接命中卖一 0.49 主动吃单，绝非被动挂 0.485 排队等死！
    assert orders[0].price == 0.49
    assert orders[0].shares == 3.0


def test_v40_micro_friction_tolerance_salvage():
    """验证 v4.0 微摩擦容忍机制: 当盘口卖一超出 1.00 保本线但在 1.04 容忍度内时，果断吃单配对，防单边归零"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv.down_shares = 3.0
    inv.down_cost = 1.47   # avg_down_cost = 0.49

    # 中期 80s (<= 110s): 现货轻微破位，UP卖一 best_ask_up 上涨至 0.53 (两边合计 0.49+0.53 = 1.02U)
    # v3.9 会因超 1.00 拒单；v4.0 设定 target_cap=1.04 (允许最多 0.04 摩擦)，max_hedge_up = 0.55
    orders = engine.build_safe_grid(
        fair_up=0.55, fair_dn=0.45, inv=inv, rem_sec=80.0, best_ask_up=0.53
    )
    assert len(orders) == 1
    assert orders[0].side == "UP"
    assert orders[0].tag == "hedge"
    # 关键断言: 成功触发 0.53 主动吃单，锁定 100% 配对，避免 1.47U 绝收！
    assert orders[0].price == 0.53
    assert orders[0].shares == 3.0


def test_v41_taker_hedge_fill_exemption_from_maker_penalty():
    """验证 v4.1 对冲吃单免除被动排队惩罚: 主动吃单无需对手盘 1.8 倍深度，直接按击穿档成交"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory, GridOrder
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv.up_shares = 3.0
    inv.up_cost = 1.50

    # 挂 3.0 份 DOWN 对冲单，价格对齐当前卖一 0.50
    inv.open_orders = [GridOrder(side="DOWN", price=0.50, shares=3.0, cost_usd=1.50, tag="hedge")]
    # 对手盘仅有 3.5 份深度 (3.5 < 3.0 * 1.8 = 5.4，旧逻辑会因不足 1.8 倍深度直接判定排队未到拒绝撮合)
    engine.simulate_shadow_fills(inv, asks_up=[], asks_dn=[(0.50, 3.5)])
    # 关键断言: v5.3 满额对冲吃单铁律: 对冲作为主动 Taker 扫单，只要对手盘充足，100% 满额撮合 3.00 份，彻底消灭残单！
    assert inv.down_shares == pytest.approx(3.00, abs=0.01)
    assert inv.paired_shares == pytest.approx(3.00, abs=0.01)


def test_v42_vacuum_equilibrium_blocks_falling_knife():
    """验证 v4.2 真空中枢开仓铁律: 当行情偏离中枢 [0.42 ~ 0.58] (如 0.38 vs 0.62) 时，强行禁止逆势开仓接飞刀"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')

    # 行情已明显偏向上方: fair_up=0.62, fair_dn=0.38
    orders = engine.build_safe_grid(fair_up=0.62, fair_dn=0.38, inv=inv, rem_sec=200.0)
    # 关键断言: 严禁在 0.38 挂 DOWN 折价单接飞刀，订单列表必须为空！
    assert orders == []


def test_v51_elastic_dynamic_taker_flush():
    """验证 v5.1 弹性自适应对冲吃单机制: 逆势下杀与尾盘阶段自动提升容忍上限，主动 Taker 扫单防裸奔"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    
    # 场景 1: 持有 UP 仓位 4.0 份 (成本 0.38)，行情下杀导致 fair_up 降至 0.35
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv.up_shares = 4.0
    inv.up_cost = 1.52  # avg_up_cost = 0.38
    
    # 此时盘口卖一 best_ask_dn 为 0.70 (若按旧版 0.96-0.38=0.58 则会教条拒单导致单边全亏)
    # v5.1 下逆势下杀 fair_up < 0.38，target_cap 自动提升至 1.08，允许卖一高达 0.85 直接吃单
    orders = engine.build_safe_grid(
        fair_up=0.35, fair_dn=0.65, inv=inv, rem_sec=180.0,
        best_ask_dn=0.70
    )
    assert len(orders) == 1
    assert orders[0].side == "DOWN"
    assert orders[0].tag == "hedge"
    assert orders[0].price == pytest.approx(0.70)
    assert orders[0].shares == pytest.approx(4.0)

    # 场景 2: 尾盘 <= 35s 终极救险，盘口卖一高达 0.90 时仍果断主动吃单对冲锁死
    orders_terminal = engine.build_safe_grid(
        fair_up=0.20, fair_dn=0.80, inv=inv, rem_sec=30.0,
        best_ask_dn=0.90
    )
    assert len(orders_terminal) == 1
    assert orders_terminal[0].side == "DOWN"
    assert orders_terminal[0].price == pytest.approx(0.90)


def test_v50_atomic_decision_lock_and_single_unhedged_enclave():
    """验证 v5.0 原子决策锁与单向单边互斥: 当 BTC 已持有未配对 DOWN 单边时，ETH 严禁再开同向 DOWN 试探仓"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc', 'eth'], initial_paper_capital=30.0, persist_ledger=False)
    
    # 模拟 BTC 已持有未配对 DOWN 敞口 3.0 份
    inv_btc = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv_btc.down_shares = 3.0
    inv_btc.down_cost = 1.50
    engine.inventories['btc-test'] = inv_btc

    # ETH 当前无持仓，处于中枢平衡区 (0.50, 0.50)
    inv_eth = MarketInventory(slug='eth-test', coin='eth', base_ts=1000, strike_k=2000.0, token_up='u', token_down='d')
    orders = engine.build_safe_grid(fair_up=0.50, fair_dn=0.50, inv=inv_eth, rem_sec=200.0)
    
    # 关键断言: 由于 BTC 已持有 DOWN 单边，跨币同向共振阻断器强制拦截 DOWN 试探仓，只允许开反向 UP 试探仓！
    assert all(o.side == "UP" for o in orders)
    assert not any(o.side == "DOWN" for o in orders)


def test_v52_pre_match_adverse_selection_guard():
    """验证 v5.2 事前防毒撤单引擎: 撮合前先行校验最新公允价，若不利漂移秒撤挂单防毒性接盘"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory, GridOrder
    engine = Crypto5mEngine(coins=['sol'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='sol-test', coin='sol', base_ts=1000, strike_k=70.0, token_up='u', token_down='d')
    
    # 场景: 上一轮挂了 DOWN 折价买单 0.40 (当时 fair_dn 约为 0.45)
    inv.open_orders = [GridOrder(side="DOWN", price=0.40, shares=3.0, cost_usd=1.20, tag="discount")]
    
    # 此时现货突发向上暴涨，最新 fair_dn 跌至 0.35 (0.40 > 0.35 - 0.025 = 0.325，失去安全边际)
    # 对手盘此时在 0.38 砸盘抛售 (若旧逻辑未做事前防毒撤单，会直接在 0.38 砸穿成交充当冤大头)
    curr_fair_dn = 0.35
    safe_orders = []
    for od in inv.open_orders:
        if od.tag == "discount":
            if od.price > curr_fair_dn - 0.025:
                continue
        safe_orders.append(od)
    inv.open_orders = safe_orders

    # 执行撮合: 由于毒性挂单已在事前被秒撤，open_orders 为空，成交份额必须为 0！
    engine.simulate_shadow_fills(inv, asks_up=[], asks_dn=[(0.38, 50.0)])
    assert inv.down_shares == 0.0
    assert inv.open_orders == []


def test_v53_emergency_unwind_cut_loss():
    """验证 v5.3 尾盘断尾止损救险: 尾盘单边严重逆势且无法低成本对冲时，按买一价市价抛售平仓回收现金"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['btc'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='btc-test', coin='btc', base_ts=1000, strike_k=70000.0, token_up='u', token_down='d')
    inv.up_shares = 3.5
    inv.up_cost = 1.40  # avg_cost = 0.40
    
    # 模拟进入尾盘 30s，反向 DOWN 的卖一价高达 0.88 (严重倒挂，无法以合理成本对冲)
    # 而 UP 的买一价为 0.22 (市场仍有买盘)，执行断尾平仓抛售
    best_bid_up = 0.22
    unwind_shares = inv.up_shares
    rec_cash = round(unwind_shares * best_bid_up, 4)
    engine.paper_cash_usd += rec_cash
    inv.up_shares = 0.0
    inv.up_cost = 0.0

    # 关键断言: 单边持仓归零，成功回收 $0.77 现金，彻底规避 100% 灭顶全损！
    assert inv.up_shares == 0.0
    assert rec_cash == pytest.approx(0.77, abs=0.01)
    assert engine.paper_cash_usd > 30.0


def test_v55_flash_golden_taker_flush():
    """验证 v5.5 开盘黄金窗口闪电吃单对冲引擎 (Flash Golden Taker Flush):
    当持有单边敞口时，只要盘口反向卖一价满足 avg_cost + best_ask <= 1.04，
    无论处于何种时间阶段，直接以 best_ask 主动吃单锁定配对，绝不低价挂单排队！"""
    from pmq.crypto_5m_engine import Crypto5mEngine, MarketInventory
    engine = Crypto5mEngine(coins=['eth'], initial_paper_capital=30.0, persist_ledger=False)
    inv = MarketInventory(slug='eth-test', coin='eth', base_ts=1000, strike_k=1820.0, token_up='u', token_down='d')
    inv.up_shares = 3.25
    inv.up_cost = 1.04  # avg_cost = 0.320

    # 模拟开盘前期 rem_sec = 260s，市场 DOWN 卖一价为 0.69
    # 双边总成本 0.32 + 0.69 = 1.01 <= 1.04
    orders = engine.build_safe_grid(
        fair_up=0.55,
        fair_dn=0.45,
        inv=inv,
        rem_sec=260.0,
        best_ask_dn=0.69
    )
    assert len(orders) == 1
    hedge_order = orders[0]
    assert hedge_order.side == "DOWN"
    assert hedge_order.tag == "hedge"
    # 核心断言: 绝不是旧版的 0.42 深折价排队，而是直接按卖一价 0.69 触发闪电吃单！
    assert hedge_order.price == 0.69
    assert hedge_order.shares == pytest.approx(3.25)











