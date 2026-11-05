"""
market_scoring.py — 选市评分骨架（纸面地基，阶段 2 前置）

依据：《Polymarket 做市交 Poly 执行方案》第 5 节（选市逻辑）
- §5.1 一票否决：任一命中即出局，先于评分执行；
- §5.2 100 分评分模型：六维度权重 25/20/15/20/10/10，分数段动作写死：
  ≥80 主力（标准份额）／65–79 观察（半份试挂）／50–64 只纸面跟踪／<50 放弃。

输入口径：
- market：sdk_adapter.market_to_dict 的输出结构（Market dict）；
- books：{token_id: sdk_adapter.orderbook_to_dict 输出}，两腿各一份，可缺；
- rewards / history / activity / related / manual_flags：可选外部数据，
  分别对应奖励池信息、markout 历史统计、成交活跃度统计、相关敞口、
  人工研判标记。本骨架不抓取这些数据，只消费调用方传入的值。

缺失数据口径（铁律）：需要外部/历史数据才能算的项，数据缺失时
该项记 0 分并在 breakdown 与 missing_fields 中标注 missing，
严禁用假数据、默认值或插值填充。否决项同理分三态：
hit（命中）／clear（已核查通过）／unknown（数据缺失、无法判定，
不触发否决但写入 veto_unknown 供人工复核）——唯一例外是 §5.1-1：
方案原文把「政策不明」本身列为否决条件，故 rewards 缺失即按
政策不明命中否决。

数值阈值来源分两类，代码中逐项注明：
- 【方案】：方案原文给出的数字（权重、3% 价差、48h、20% 占盘口、
  5 倍深度、7–60 天甜区、分数段等）；
- 【自拟】：方案只给方向未给数值的映射断点（奖励比/活跃度等），
  以模块常量给出默认值并在交付说明登记，待纸面实测校准后改常量，
  不改结构。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

__all__ = [
    "score_market",
    "VETO_SPREAD_PCT",
    "SCORE_BANDS",
]

# ---------------------------------------------------------------------------
# 【方案】常量（出处：方案 §5.1 / §5.2 / §6.2）
# ---------------------------------------------------------------------------

VETO_SPREAD_PCT = 3.0            # §5.1-2：盘口价差长期 >3% 否决
VETO_MIN_HOURS_TO_EXPIRY = 48.0  # §5.1-4：距到期 <48 小时否决
VETO_DEPTH_DOMINANCE = 0.20      # §5.1-6：目标份额占盘口 >20% 否决
SWEET_SPREAD_LO_PCT = 1.0        # §5.2 价差甜区下限
SWEET_SPREAD_HI_PCT = 2.5        # §5.2 价差甜区上限
DEPTH_FULL_MULTIPLE = 5.0        # §5.2：深度够目标份额 5 倍满分
SWEET_EXPIRY_MIN_DAYS = 7.0      # §5.2 到期甜区
SWEET_EXPIRY_MAX_DAYS = 60.0
DEFAULT_TARGET_SHARES = 20.0     # §5.2/§6.2：奖励门槛份额起步常见 20 股

# §5.2 分数段动作（门槛写死，不许调用方临时改口径）
SCORE_BANDS = [
    (80.0, "main", "主力市场：可挂标准份额"),
    (65.0, "watch", "观察市场：半份试挂"),
    (50.0, "paper", "只纸面跟踪"),
    (0.0, "drop", "放弃"),
]

# ---------------------------------------------------------------------------
# 【自拟】常量（方案未给数值，纸面校准前默认值，交付说明已登记）
# ---------------------------------------------------------------------------

SELF_MIN_VOLUME24HR_USD = 2_000.0   # §5.1-3「日成交量过低」的暂定下限（USD/24h）
SELF_REWARD_RATIO_FULL_PCT = 1.0    # 奖励日额/锁定资金 ≥1%/日 → 奖励维满分
SELF_ACTIVITY_TRADES_FULL = 200.0   # 日成交笔数 ≥200 → 活跃度笔数分满分
SELF_PULSE_CV = 1.5                 # 成交量变异系数 ≥1.5 视为脉冲式暴量


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _parse_end_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _spread_pct(market: Dict, books: Optional[Dict]) -> Optional[float]:
    """当前盘口价差（%）。优先 Book（YES 腿 best bid/ask），回退 Market 字段。"""
    if books:
        for book in books.values():
            bid, ask = book.get("best_bid"), book.get("best_ask")
            if bid is not None and ask is not None and ask > 0:
                return (ask - bid) * 100.0
    bid, ask = market.get("best_bid"), market.get("best_ask")
    if bid is not None and ask is not None and ask > 0:
        return (ask - bid) * 100.0
    return None


def _side_depth_shares(book: Dict) -> float:
    """单侧可见深度（股）：bids/asks 两侧取较小者（短板决定可挂性）。"""
    bid_shares = sum(level["size"] for level in book.get("bids", []))
    ask_shares = sum(level["size"] for level in book.get("asks", []))
    return min(bid_shares, ask_shares)


# ---------------------------------------------------------------------------
# §5.1 一票否决（逐项与方案编号对照）
# ---------------------------------------------------------------------------

def _run_vetoes(market, books, rewards, history, related, manual_flags,
                target_shares, now):
    """返回 (hits, unknowns)。每项三态：hit / clear / unknown。"""
    hits: List[Dict] = []
    unknowns: List[Dict] = []

    def hit(item, reason):
        hits.append({"item": item, "reason": reason})

    def unknown(item, reason):
        unknowns.append({"item": item, "reason": reason})

    # §5.1-1 无流动性奖励或奖励池已取消/政策不明
    # 方案原文含「政策不明」→ rewards 缺失按命中处理（本清单唯一例外）。
    if not rewards or not rewards.get("has_rewards"):
        hit("5.1-1", "无流动性奖励或奖励信息缺失（政策不明）")
    elif rewards.get("pool_status") in ("cancelled", "unknown"):
        hit("5.1-1", f"奖励池状态={rewards['pool_status']}")

    # §5.1-2 盘口价差长期 >3%（有历史中位数优先用历史，否则用当前快照并注明）
    hist_spread = (history or {}).get("spread_median_pct")
    cur_spread = _spread_pct(market, books)
    if hist_spread is not None:
        if hist_spread > VETO_SPREAD_PCT:
            hit("5.1-2", f"历史价差中位数 {hist_spread:.2f}% > 3%")
    elif cur_spread is not None:
        if cur_spread > VETO_SPREAD_PCT:
            hit("5.1-2", f"当前价差 {cur_spread:.2f}% > 3%（无历史中位数，以快照判定）")
    else:
        unknown("5.1-2", "无盘口价差数据")

    # §5.1-3 日成交量过低（阈值【自拟】），或过高且波动剧烈
    vol24 = market.get("volume24hr")
    if vol24 is None:
        unknown("5.1-3", "无 24h 成交量数据")
    elif vol24 < SELF_MIN_VOLUME24HR_USD:
        hit("5.1-3", f"24h 成交量 ${vol24:,.0f} < 下限 ${SELF_MIN_VOLUME24HR_USD:,.0f}【自拟阈值】")
    # 「过高且波动剧烈」需要量能波动统计，归 history/activity，缺失不判定

    # §5.1-4 距到期 <48 小时
    end_dt = _parse_end_date(market.get("end_date") or "")
    if end_dt is None:
        unknown("5.1-4", "无到期时间数据")
    elif end_dt - now < timedelta(hours=VETO_MIN_HOURS_TO_EXPIRY):
        hit("5.1-4", f"距到期 {(end_dt - now).total_seconds() / 3600:.1f}h < 48h")

    # §5.1-5 信息高度不对称（人工标记）＋ markout 历史为负 → 永久拉黑
    flags = manual_flags or {}
    if flags.get("insider_sensitive"):
        hit("5.1-5", "人工标记：事件信息高度不对称/内幕敏感")
    med_markout = (history or {}).get("markout_5m_median")
    if med_markout is not None and med_markout < 0:
        hit("5.1-5", f"markout(5m) 历史中位数 {med_markout} < 0，永久拉黑")

    # §5.1-6 盘口深度极薄：目标份额占盘口 >20%
    if books:
        for tid, book in books.items():
            depth = _side_depth_shares(book)
            if depth > 0 and target_shares / depth > VETO_DEPTH_DOMINANCE:
                hit("5.1-6", f"token {tid} 目标份额占单侧深度 {target_shares / depth:.0%} > 20%")
                break
            if depth <= 0:
                hit("5.1-6", f"token {tid} 单侧深度为 0（空盘）")
                break
    else:
        unknown("5.1-6", "无订单簿深度数据")

    # §5.1-7 规则/结算口径含糊（人工标记）
    if flags.get("ambiguous_settlement"):
        hit("5.1-7", "人工标记：规则/结算口径含糊")

    # §5.1-8 同一事件相关敞口超限（需组合层输入，§6.2 总库存 ≤60% 口径）
    if related is not None:
        total_after = related.get("total_exposure_pct_after")
        if total_after is not None and total_after > 60.0:
            hit("5.1-8", f"加入后总相关敞口 {total_after:.1f}% > 60%（§6.2）")
    else:
        unknown("5.1-8", "无组合相关敞口数据")

    return hits, unknowns


# ---------------------------------------------------------------------------
# §5.2 评分（六维度，权重 25/20/15/20/10/10）
# ---------------------------------------------------------------------------

def _score_rewards(rewards, target_shares, books):
    """奖励性价比（25）。需 rewards.daily_reward_usd 与锁定资金估算。"""
    if not rewards or rewards.get("daily_reward_usd") is None:
        return 0.0, ["rewards.daily_reward_usd"], "奖励日额数据缺失"
    # 锁定资金 ≈ 双边各挂 target_shares 所需资金 ≈ target_shares ×(YES mid + NO mid)，
    # 无盘口时按完整集 ≈1.0 估算（口径注明，非假数据填充：仅作分母估算）。
    locked = float(target_shares)
    if books and len(books) >= 2:
        mids = []
        for book in books.values():
            bid, ask = book.get("best_bid"), book.get("best_ask")
            if bid is not None and ask is not None:
                mids.append((bid + ask) / 2.0)
        if len(mids) == 2:
            locked = float(target_shares) * sum(mids)
    ratio_pct = rewards["daily_reward_usd"] / locked * 100.0 if locked > 0 else 0.0
    score = min(ratio_pct / SELF_REWARD_RATIO_FULL_PCT, 1.0) * 25.0
    note = f"奖励日额/锁定资金={ratio_pct:.3f}%/日（满分线 {SELF_REWARD_RATIO_FULL_PCT}%【自拟】）"
    return round(score, 2), [], note


def _score_spread_depth(market, books, target_shares):
    """价差与深度（20）＝价差 10 + 深度 10。"""
    missing: List[str] = []
    notes: List[str] = []
    spread = _spread_pct(market, books)
    if spread is None:
        spread_score = 0.0
        missing.append("book.best_bid/best_ask")
        notes.append("价差数据缺失")
    elif SWEET_SPREAD_LO_PCT <= spread <= SWEET_SPREAD_HI_PCT:
        spread_score = 10.0
        notes.append(f"价差 {spread:.2f}% 在甜区 1–2.5%")
    elif spread < SWEET_SPREAD_LO_PCT:
        spread_score = 7.0  # 【自拟】价差过薄：肉少但无毒，次档
        notes.append(f"价差 {spread:.2f}% < 1%，边际薄")
    else:  # 2.5% < spread ≤ 3%（>3% 已被否决）
        spread_score = 5.0  # 【自拟】贴近否决线
        notes.append(f"价差 {spread:.2f}% 在 2.5–3% 警戒区")
    if books:
        worst = min(_side_depth_shares(b) for b in books.values())
        multiple = worst / target_shares if target_shares > 0 else 0.0
        depth_score = min(multiple / DEPTH_FULL_MULTIPLE, 1.0) * 10.0
        notes.append(f"最薄单侧深度为目标份额 {multiple:.1f} 倍（满分 5 倍【方案】）")
    else:
        depth_score = 0.0
        missing.append("book.depth")
        notes.append("深度数据缺失")
    return round(spread_score + depth_score, 2), missing, "；".join(notes)


def _score_activity(activity):
    """成交活跃度（15）＝笔数 10 + 稳定性 5。需 activity 统计，缺失 0 分。"""
    if not activity or activity.get("trades_per_day") is None:
        return 0.0, ["activity.trades_per_day"], "成交活跃度统计缺失"
    trades = float(activity["trades_per_day"])
    score = min(trades / SELF_ACTIVITY_TRADES_FULL, 1.0) * 10.0
    missing: List[str] = []
    notes = [f"日成交笔数 {trades:.0f}（满分线 {SELF_ACTIVITY_TRADES_FULL:.0f}【自拟】）"]
    cv = activity.get("volume_cv")
    if cv is None:
        missing.append("activity.volume_cv")
        notes.append("量能波动数据缺失，稳定性 5 分记 0")
    else:
        # 【自拟】cv ≤0.5 满分，≥SELF_PULSE_CV（脉冲）0 分，线性
        stab = max(0.0, min(1.0, (SELF_PULSE_CV - cv) / (SELF_PULSE_CV - 0.5))) * 5.0
        score += stab
        notes.append(f"量能 CV={cv:.2f}，稳定性分 {stab:.1f}/5")
    return round(score, 2), missing, "；".join(notes)


def _score_markout(history):
    """逆选择风险（20）。markout(5m) 中位数 ≥0 满分【方案】；
    为负者在否决阶段已被 §5.1-5 拉黑，不会走到评分；缺失 0 分。"""
    med = (history or {}).get("markout_5m_median")
    if med is None:
        return 0.0, ["history.markout_5m_median"], "markout 历史缺失（新市场常态，纸面期积累）"
    if med >= 0:
        return 20.0, [], f"markout(5m) 中位数 {med} ≥ 0，满分【方案】"
    return 0.0, [], f"markout(5m) 中位数 {med} < 0（应已在否决阶段拉黑）"


def _score_expiry_type(market, event_type, now):
    """到期距离与事件类型（10）＝距离 6 + 类型 4。"""
    missing: List[str] = []
    notes: List[str] = []
    end_dt = _parse_end_date(market.get("end_date") or "")
    if end_dt is None:
        dist_score = 0.0
        missing.append("market.end_date")
        notes.append("到期时间缺失")
    else:
        days = (end_dt - now).total_seconds() / 86400.0
        if SWEET_EXPIRY_MIN_DAYS <= days <= SWEET_EXPIRY_MAX_DAYS:
            dist_score = 6.0
            notes.append(f"距到期 {days:.0f} 天，在甜区 7–60 天【方案】")
        elif days > SWEET_EXPIRY_MAX_DAYS:
            dist_score = 4.0  # 【自拟】长尾：可做但资金占用久
            notes.append(f"距到期 {days:.0f} 天 > 60 天，长尾")
        else:  # 48h ≤ 距到期 < 7 天（<48h 已否决）
            dist_score = 3.0  # 【自拟】临近到期收尾区
            notes.append(f"距到期 {days:.1f} 天 < 7 天")
    # 【方案】体育/天气等高频可验证类优于长尾政治类；档位分【自拟】
    type_scores = {"sports": 4.0, "weather": 4.0, "crypto": 3.0,
                   "politics": 2.0, "other": 2.0}
    if event_type is None:
        type_score = 0.0
        missing.append("event_type")
        notes.append("事件类型缺失（适配层不提供分类，由消费方传入）")
    else:
        type_score = type_scores.get(event_type, 2.0)
        notes.append(f"事件类型 {event_type} → {type_score:.0f}/4【自拟档位】")
    return round(dist_score + type_score, 2), missing, "；".join(notes)


def _score_inventory(books):
    """库存可管理性（10）＝两腿对称 5 + 补腿成本可估 5。需两腿订单簿。"""
    if not books or len(books) < 2:
        return 0.0, ["books.both_legs"], "两腿订单簿缺失"
    depths = [_side_depth_shares(b) for b in books.values()]
    lo, hi = min(depths), max(depths)
    symmetry = (lo / hi) if hi > 0 else 0.0
    sym_score = symmetry * 5.0
    # 补腿成本可估：两腿都有卖盘（可按 ask 补腿）记满分，缺一腿打折【自拟】
    legs_with_ask = sum(1 for b in books.values() if b.get("asks"))
    comp_score = 5.0 if legs_with_ask == 2 else (2.0 if legs_with_ask == 1 else 0.0)
    note = f"两腿深度对称度 {symmetry:.2f}；有卖盘腿数 {legs_with_ask}/2"
    return round(sym_score + comp_score, 2), [], note


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def score_market(market: Dict, *, books: Optional[Dict] = None,
                 rewards: Optional[Dict] = None,
                 history: Optional[Dict] = None,
                 activity: Optional[Dict] = None,
                 related: Optional[Dict] = None,
                 manual_flags: Optional[Dict] = None,
                 event_type: Optional[str] = None,
                 target_shares: float = DEFAULT_TARGET_SHARES,
                 now: Optional[datetime] = None) -> Dict:
    """对单个市场执行 §5.1 否决 + §5.2 评分。

    参数（除 market 外全部可选，缺失按模块 docstring 的缺失口径处理）：
    - books：{token_id: Book dict}（sdk_adapter.orderbook_to_dict 输出）
    - rewards：{"has_rewards": bool, "pool_status": str,
                "daily_reward_usd": float}
    - history：{"markout_5m_median": float（价格单位）,
                "spread_median_pct": float}
    - activity：{"trades_per_day": float, "volume_cv": float}
    - related：{"total_exposure_pct_after": float}（加入本市场后的总相关敞口 %）
    - manual_flags：{"insider_sensitive": bool, "ambiguous_settlement": bool}
    - event_type：sports/weather/crypto/politics/other
    - now：判定期时间（自测注入），默认当前 UTC。
    """
    now = now or datetime.now(timezone.utc)
    veto_hits, veto_unknowns = _run_vetoes(
        market, books, rewards, history, related, manual_flags, target_shares, now)

    result: Dict = {
        "market_id": market.get("id"),
        "question": market.get("question"),
        "vetoed": bool(veto_hits),
        "veto_hits": veto_hits,
        "veto_unknown": veto_unknowns,
        "score": None,
        "breakdown": [],
        "band": None,
        "action": None,
        "missing_fields": [],
    }
    if veto_hits:
        result["action"] = "一票否决出局（§5.1），不进入评分"
        return result

    dims = [
        ("奖励性价比", "5.2", 25, _score_rewards(rewards, target_shares, books)),
        ("价差与深度", "5.2", 20, _score_spread_depth(market, books, target_shares)),
        ("成交活跃度", "5.2", 15, _score_activity(activity)),
        ("逆选择风险", "5.2", 20, _score_markout(history)),
        ("到期距离与事件类型", "5.2", 10, _score_expiry_type(market, event_type, now)),
        ("库存可管理性", "5.2", 10, _score_inventory(books)),
    ]
    total = 0.0
    for name, section, max_score, (score, missing, note) in dims:
        total += score
        result["breakdown"].append({
            "dimension": name, "section": section, "max": max_score,
            "score": score, "missing": missing, "note": note,
        })
        result["missing_fields"].extend(missing)
    result["score"] = round(total, 2)
    for threshold, band, action in SCORE_BANDS:
        if total >= threshold:
            result["band"] = band
            result["action"] = action
            break
    return result
