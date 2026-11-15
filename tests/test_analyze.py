import pytest

from pmq import analyze
from pmq.collect import Snapshot


def _snap(ts, b0, b1, a0=None, a1=None, mid="m1"):
    return {"ts": ts, "market_id": mid, "question": "q",
            "bid_0": b0, "ask_0": a0 if a0 is not None else b0 + 0.01,
            "bid_1": b1, "ask_1": a1 if a1 is not None else b1 + 0.01}


def test_snapshot_properties():
    s = Snapshot(ts=1.0, market_id="m", question="q", bid_0=0.47, ask_0=0.49,
                 bid_1=0.48, ask_1=0.50)
    assert s.bid_sum == pytest.approx(0.95)
    assert s.mid_sum == pytest.approx(0.97)


def test_markout_negative_when_bids_fall():
    series = {"m1": [_snap(1000, 0.47, 0.48), _snap(1060, 0.46, 0.47)]}
    rows = analyze.markout_rows(series, horizons=[60])
    assert len(rows) == 1
    assert rows[0]["markout_pct"] < 0


def test_markout_positive_when_bids_rise():
    series = {"m1": [_snap(1000, 0.47, 0.48), _snap(1060, 0.48, 0.49)]}
    rows = analyze.markout_rows(series, horizons=[60])
    assert rows[0]["markout_pct"] > 0


def test_markout_skips_when_no_later_sample():
    series = {"m1": [_snap(1000, 0.47, 0.48)]}
    assert analyze.markout_rows(series, horizons=[60]) == []


def test_markout_tolerance_rejects_far_samples():
    series = {"m1": [_snap(1000, 0.47, 0.48), _snap(1900, 0.46, 0.47)]}
    assert analyze.markout_rows(series, horizons=[60], tol_ratio=0.25) == []


def test_summarize_reports_distribution():
    rows = [
        {"horizon": 60, "markout_pct": -1.0},
        {"horizon": 60, "markout_pct": 1.0},
        {"horizon": 60, "markout_pct": -3.0},
    ]
    out = analyze.summarize(rows)
    assert out[0]["samples"] == 3
    assert out[0]["mean_pct"] == pytest.approx(-1.0)
    assert out[0]["negative_share"] == pytest.approx(66.7, abs=0.1)
    assert out[0]["min_pct"] == -3.0
