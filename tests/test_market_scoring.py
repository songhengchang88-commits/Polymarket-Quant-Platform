"""Scoring tests (local v20261112-1 market_scoring.py).

Audit: P1-12 unknown does not veto (except 5.1-1/5.1-6), P1-13 band
by attainable-max ratio with watch cap, P1-3 target_shares can
manipulate veto/score, P2 markout score is binary, OI-13 empty books
vetoes (local fix; public repo old code only records unknown).
Pure function score_market only, no network, no live orders.
"""
from datetime import datetime, timedelta, timezone

from market_scoring import score_market

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def book(bid=0.49, ask=0.51, size=1000.0):
    return {
        "best_bid": bid, "best_ask": ask,
        "bids": [{"price": bid, "size": size}],
        "asks": [{"price": ask, "size": size}],
    }


def books():
    return {"YES": book(), "NO": book()}


def market(end_days=30, volume=100_000.0):
    return {
        "id": "m1",
        "question": "test market?",
        "end_date": (NOW + timedelta(days=end_days)).isoformat(),
        "volume24hr": volume,
        "best_bid": 0.49,
        "best_ask": 0.51,
        "clob_token_ids": ["YES", "NO"],
    }


def rewards():
    return {"has_rewards": True, "pool_status": "active",
            "daily_reward_usd": 5.0}


def history(markout=0.01, spread=2.0):
    return {"markout_5m_median": markout, "spread_median_pct": spread}


def activity():
    return {"trades_per_day": 500.0, "volume_cv": 0.3}


def full_kwargs(**over):
    kw = dict(books=books(), rewards=rewards(), history=history(),
              activity=activity(),
              related={"total_exposure_pct_after": 10.0},
              event_type="sports", now=NOW)
    kw.update(over)
    return kw


def hit_items(result):
    return [h["item"] for h in result["veto_hits"]]


def unknown_items(result):
    return [u["item"] for u in result["veto_unknown"]]


def test_veto_no_rewards_is_hard_and_score_is_none():
    r = score_market(market(), **full_kwargs(rewards=None))
    assert r["vetoed"] is True
    assert r["score"] is None
    assert r["band"] is None
    assert "5.1-1" in hit_items(r)


def test_veto_spread_over_3pct():
    # NOTE: history.spread_median_pct (2.0%) takes precedence over the
    # snapshot spread, so history must be None here for the 4% snapshot
    # to be the deciding input.
    b = {"YES": book(0.48, 0.52), "NO": book(0.48, 0.52)}
    r = score_market(market(), **full_kwargs(books=b, history=None))
    assert r["vetoed"] is True
    assert "5.1-2" in hit_items(r)


def test_veto_expiry_under_48h():
    r = score_market(market(end_days=0), **full_kwargs())
    assert r["vetoed"] is True
    assert "5.1-4" in hit_items(r)


def test_veto_negative_markout_history():
    r = score_market(market(), **full_kwargs(history=history(markout=-0.03)))
    assert r["vetoed"] is True
    assert "5.1-5" in hit_items(r)


def test_veto_empty_books_oi13_local_fix():
    r = score_market(market(), **full_kwargs(books=None))
    assert r["vetoed"] is True
    assert "5.1-6" in hit_items(r)


def test_veto_depth_dominance_over_20pct():
    thin = {"YES": book(size=30.0), "NO": book(size=30.0)}
    r = score_market(market(), **full_kwargs(books=thin))
    assert r["vetoed"] is True
    assert "5.1-6" in hit_items(r)


def test_veto_related_exposure_over_60pct():
    r = score_market(
        market(), **full_kwargs(related={"total_exposure_pct_after": 70.0}))
    assert r["vetoed"] is True
    assert "5.1-8" in hit_items(r)


def test_full_inputs_not_vetoed_and_scored():
    r = score_market(market(), **full_kwargs())
    assert r["vetoed"] is False
    assert r["score"] is not None
    assert r["band"] in ("main", "watch", "paper", "drop")


def test_unknown_does_not_veto_known_issue():
    # KNOWN ISSUE P1-12: except 5.1-1/5.1-6, missing data only lands in
    # veto_unknown and does not veto. Fix: block/degrade when key
    # dimensions are missing; then change this test.
    m = market()
    del m["end_date"]
    r = score_market(m, books=books(), rewards=rewards(), history=None,
                     activity=None, related=None, event_type=None, now=NOW)
    assert r["vetoed"] is False
    assert r["score"] is not None
    assert "5.1-4" in unknown_items(r)
    assert "5.1-8" in unknown_items(r)


def test_target_shares_1_manipulates_veto_and_score_known_issue():
    # KNOWN ISSUE P1-3: same market, same books (30 shares/side):
    # target 20 is 66% of depth -> 5.1-6 veto; target 1 is 3.3% ->
    # no veto and near-full depth score, while the real order is
    # still 20 shares (20x the scored assumption). Fix: validate
    # target_shares against the real order size.
    thin = {"YES": book(size=30.0), "NO": book(size=30.0)}
    r20 = score_market(market(), **full_kwargs(books=thin, target_shares=20.0))
    assert r20["vetoed"] is True
    assert "5.1-6" in hit_items(r20)
    r1 = score_market(market(), **full_kwargs(books=thin, target_shares=1.0))
    assert r1["vetoed"] is False
    depth_dim = [d for d in r1["breakdown"] if d["dimension"] == "价差与深度"][0]
    assert depth_dim["score"] >= 19.0


def test_markout_score_is_binary_no_gradient_known_issue():
    # KNOWN ISSUE P2: markout +0.001 and +10 both score 20/20, no
    # gradient; missing scores 0 but does not veto.
    r_small = score_market(market(), **full_kwargs(history=history(markout=0.001)))
    r_big = score_market(market(), **full_kwargs(history=history(markout=10.0)))

    def markout_dim(r):
        return [d for d in r["breakdown"] if d["dimension"] == "逆选择风险"][0]

    assert markout_dim(r_small)["score"] == 20.0
    assert markout_dim(r_big)["score"] == 20.0
    r_none = score_market(market(), **full_kwargs(history=None))
    assert r_none["vetoed"] is False
    assert markout_dim(r_none)["score"] == 0.0


def test_partial_coverage_capped_at_watch():
    r = score_market(market(), books=books(), rewards=rewards(),
                     history=None, activity=None, related=None,
                     event_type=None, now=NOW)
    assert r["vetoed"] is False
    assert r["score_attainable_max"] == 61.0
    assert r["score"] == 61.0
    assert r["band"] == "watch"
