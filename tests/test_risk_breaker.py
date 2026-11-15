"""Breaker tests (local v20261112-1 risk_breaker.py).

Audit: P0-6 decision-only breaker, P0-7 unrealized defaults to 0,
B5 cooldown-reset cumulative -44%, B6 state-file deletion bypass and
cross-day auto-recovery, P1-9. Amounts are integer micro ($200 =
200_000_000). Fixed Shanghai dates so tests do not depend on wall time.
Paper only, no network, no live orders.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from mm_ledger import MmLedger
from risk_breaker import (
    COOLDOWN_7D,
    DAILY_HALT,
    NORMAL,
    RECONCILE_HALT,
    TERMINATED,
    BreakerSnapshot,
    RiskBreaker,
    snapshot_from_ledger,
)

SH = timezone(timedelta(hours=8))
DAY1 = datetime(2026, 11, 15, 12, 0, tzinfo=SH)
M = 1_000_000


def ms(dt):
    return int(dt.timestamp() * 1000)


def snap(now=200 * M, peak=200 * M, day_start=200 * M, reconcile_ok=True):
    return BreakerSnapshot(
        equity_now_micro=now,
        equity_peak_micro=peak,
        day_start_equity_micro=day_start,
        reconcile_ok=reconcile_ok,
    )


def breaker(tmp_path):
    return RiskBreaker(str(tmp_path))


def test_normal_allows_open_full_scale(tmp_path):
    d = breaker(tmp_path).evaluate(snap(), now_ms=ms(DAY1))
    assert d.state == NORMAL
    assert d.can_open is True
    assert d.reduce_only is False
    assert d.position_scale == 1.0


def test_daily_loss_exactly_at_2pct_of_current_equity_halts(tmp_path):
    # Daily limit denominator is CURRENT equity (P2 note), so the
    # trigger boundary is NOT $196.0784: loss >= floor(equity*2/100)
    # solves to equity <= 196078432 (loss $3,921,568 = limit $3,921,568).
    b = breaker(tmp_path)
    d = b.evaluate(snap(now=196_078_432), now_ms=ms(DAY1))
    assert d.state == DAILY_HALT
    assert d.can_open is False
    assert d.reduce_only is True


def test_daily_loss_one_micro_short_does_not_halt(tmp_path):
    # One micro above the boundary: loss $3,921,567 < limit $3,921,568.
    b = breaker(tmp_path)
    d = b.evaluate(snap(now=196_078_433), now_ms=ms(DAY1))
    assert d.state == NORMAL
    assert d.can_open is True


def test_daily_halt_auto_recovers_next_day_known_issue(tmp_path):
    # KNOWN ISSUE P1-9: DAILY_HALT auto-recovers cross-day with no
    # review flag / human confirmation in code. Desired: recovery
    # only after review; fix the code, then change this test.
    b = breaker(tmp_path)
    b.evaluate(snap(now=196_000_000), now_ms=ms(DAY1))
    assert b.state == DAILY_HALT
    d = b.evaluate(snap(now=196_000_000, day_start=196_000_000),
                   now_ms=ms(DAY1 + timedelta(days=1)))
    assert d.state == NORMAL
    assert d.can_open is True


def test_drawdown_10pct_halves_position_not_halt(tmp_path):
    b = breaker(tmp_path)
    d = b.evaluate(snap(now=180 * M, peak=200 * M, day_start=180 * M),
                   now_ms=ms(DAY1))
    assert d.state == NORMAL
    assert d.can_open is True
    assert d.position_scale == 0.5


def test_drawdown_20pct_enters_7d_cooldown(tmp_path):
    b = breaker(tmp_path)
    d = b.evaluate(snap(now=160 * M, peak=200 * M, day_start=160 * M),
                   now_ms=ms(DAY1))
    assert d.state == COOLDOWN_7D
    assert d.can_open is False
    assert d.reduce_only is True
    assert d.cooldown_until_ms == ms(DAY1) + 7 * 24 * 3600 * 1000


def test_drawdown_30pct_terminates_and_is_sticky(tmp_path):
    b = breaker(tmp_path)
    d = b.evaluate(snap(now=140 * M, peak=200 * M, day_start=140 * M),
                   now_ms=ms(DAY1))
    assert d.state == TERMINATED
    assert d.can_open is False
    d2 = b.evaluate(snap(now=200 * M, peak=200 * M, day_start=200 * M),
                    now_ms=ms(DAY1 + timedelta(days=1)))
    assert d2.state == TERMINATED


def test_cooldown_reset_baseline_allows_cumulative_44pct_known_defect(tmp_path):
    # KNOWN DEFECT B5: $200 -> $160 (-20% cooldown) -> baseline reset
    # to $160 -> another -30% to $112 terminates, cumulative -44%
    # (-$88) vs the intended -$60. Fix: terminate on original peak /
    # initial capital dual track, then change this test.
    b = breaker(tmp_path)
    b.evaluate(snap(now=160 * M, peak=200 * M, day_start=160 * M),
               now_ms=ms(DAY1))
    assert b.state == COOLDOWN_7D
    d = b.evaluate(snap(now=160 * M, peak=200 * M, day_start=160 * M),
                   now_ms=ms(DAY1 + timedelta(days=8)))
    assert d.state == NORMAL
    d2 = b.evaluate(snap(now=112 * M, peak=200 * M, day_start=112 * M),
                    now_ms=ms(DAY1 + timedelta(days=8)))
    assert d2.state == TERMINATED


def test_reconcile_halt_requires_manual_clear(tmp_path):
    b = breaker(tmp_path)
    d = b.evaluate(snap(reconcile_ok=False), now_ms=ms(DAY1))
    assert d.state == RECONCILE_HALT
    assert d.can_open is False
    d2 = b.evaluate(snap(reconcile_ok=True), now_ms=ms(DAY1))
    assert d2.state == RECONCILE_HALT
    with pytest.raises(ValueError):
        b.clear_reconcile_halt(operator="", note="x")
    b.clear_reconcile_halt(operator="tester", note="reconciled")
    assert b.state == NORMAL


def test_daily_halt_survives_restart(tmp_path):
    b = breaker(tmp_path)
    b.evaluate(snap(now=196_000_000), now_ms=ms(DAY1))
    b2 = RiskBreaker(str(tmp_path))
    assert b2.state == DAILY_HALT
    assert len(b2.audit_log()) > 0


def test_deleted_state_file_bypasses_breaker_known_defect(tmp_path):
    # KNOWN DEFECT B6: deleting risk_breaker_state.json makes a new
    # instance return NORMAL, wiping TERMINATED/DAILY_HALT/COOLDOWN_7D.
    # Fix: missing file must halt, not reset; then change this test.
    b = breaker(tmp_path)
    b.evaluate(snap(now=140 * M, peak=200 * M, day_start=140 * M),
               now_ms=ms(DAY1))
    assert b.state == TERMINATED
    os.remove(os.path.join(str(tmp_path), RiskBreaker.FILE_NAME))
    b2 = RiskBreaker(str(tmp_path))
    assert b2.state == NORMAL


def test_corrupt_state_file_goes_to_reconcile_halt_not_crash(tmp_path):
    b = breaker(tmp_path)
    path = os.path.join(str(tmp_path), RiskBreaker.FILE_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json{{{")
    b2 = RiskBreaker(str(tmp_path))
    assert b2.state == RECONCILE_HALT
    assert any(".corrupt-" in n for n in os.listdir(str(tmp_path)))


def test_manual_reset_terminated_requires_operator_and_note(tmp_path):
    b = breaker(tmp_path)
    b.evaluate(snap(now=140 * M, peak=200 * M, day_start=140 * M),
               now_ms=ms(DAY1))
    with pytest.raises(ValueError):
        b.manual_reset_terminated(operator="tester", note="")
    b.manual_reset_terminated(operator="tester", note="Jason approved reset")
    assert b.state == NORMAL


def test_snapshot_from_ledger_defaults_unrealized_to_zero_known_issue(tmp_path):
    # KNOWN ISSUE P0-7: snapshot_from_ledger defaults unrealized to 0,
    # so a $200 cash balance with -$12 unrealised is still valued $200
    # unless the caller passes the float. Fix: derive it in the ledger.
    ledger = MmLedger("paper", str(tmp_path))
    ledger.record_cash(kind="deposit", amount_micro=200 * M)
    s = snapshot_from_ledger(
        ledger, equity_peak_micro=200 * M, day_start_equity_micro=200 * M)
    assert s.equity_now_micro == 200 * M
    s2 = snapshot_from_ledger(
        ledger, unrealized_pnl_micro=-12 * M,
        equity_peak_micro=200 * M, day_start_equity_micro=200 * M)
    assert s2.equity_now_micro == 188 * M
    assert s2.daily_pnl_micro == -12 * M
