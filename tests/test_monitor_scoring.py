"""Tests for the weighted confidence score in monitor.py."""
from __future__ import annotations

from ice_monitor.monitor import (
    CONFIDENCE_ACTIVE,
    CONFIDENCE_QUIET,
    JUMPS_THRESHOLD,
    NPC_KILLS_THRESHOLD,
    SCORE_JUMPS,
    SCORE_MINING,
    SCORE_NPC_KILLS,
    IceMonitor,
)


def test_weights_sum_to_100() -> None:
    """Documenting invariant: a 'max signal' poll scores exactly 100."""
    assert SCORE_NPC_KILLS + SCORE_MINING + SCORE_JUMPS == 100


def test_no_signals_scores_zero(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(jumps=0, npc_kills=0, mining_signal=False)
    assert score == 0
    assert signals == []


def test_only_npc_kills_at_threshold(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(
        jumps=0, npc_kills=NPC_KILLS_THRESHOLD, mining_signal=False
    )
    assert score == SCORE_NPC_KILLS
    assert len(signals) == 1
    assert "NPC kills" in signals[0]


def test_only_jumps_at_threshold(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(
        jumps=JUMPS_THRESHOLD, npc_kills=0, mining_signal=False
    )
    assert score == SCORE_JUMPS
    assert "jumps" in signals[0]


def test_only_mining_signal(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(jumps=0, npc_kills=0, mining_signal=True)
    assert score == SCORE_MINING
    assert "mining" in signals[0]


def test_just_below_npc_threshold_no_contribution(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(
        jumps=0, npc_kills=NPC_KILLS_THRESHOLD - 1, mining_signal=False
    )
    assert score == 0
    assert signals == []


def test_all_signals_maxes_score(monitor: IceMonitor) -> None:
    score, signals = monitor._confidence_score(
        jumps=JUMPS_THRESHOLD, npc_kills=NPC_KILLS_THRESHOLD, mining_signal=True
    )
    assert score == 100
    assert len(signals) == 3


def test_active_threshold_needs_two_strong_signals(monitor: IceMonitor) -> None:
    """CONFIDENCE_ACTIVE (60) requires either NPC+mining, NPC+jumps, or mining+jumps."""
    # NPC + mining = 80 → active
    score_a, _ = monitor._confidence_score(0, NPC_KILLS_THRESHOLD, True)
    # NPC + jumps = 60 → exactly at active threshold
    score_b, _ = monitor._confidence_score(JUMPS_THRESHOLD, NPC_KILLS_THRESHOLD, False)
    # mining + jumps = 60 → exactly at active threshold
    score_c, _ = monitor._confidence_score(JUMPS_THRESHOLD, 0, True)
    # jumps alone = 20 → NOT active
    score_d, _ = monitor._confidence_score(JUMPS_THRESHOLD, 0, False)

    assert score_a >= CONFIDENCE_ACTIVE
    assert score_b >= CONFIDENCE_ACTIVE
    assert score_c >= CONFIDENCE_ACTIVE
    assert score_d < CONFIDENCE_ACTIVE


def test_quiet_threshold_boundary(monitor: IceMonitor) -> None:
    """Score < CONFIDENCE_QUIET (20) counts toward clearing; exactly 20 is ambiguous."""
    # 0 signals → 0, quiet
    score_empty, _ = monitor._confidence_score(0, 0, False)
    # jumps only → 20, NOT quiet (sits in ambiguous range)
    score_jumps, _ = monitor._confidence_score(JUMPS_THRESHOLD, 0, False)

    assert score_empty < CONFIDENCE_QUIET
    assert score_jumps == CONFIDENCE_QUIET  # boundary — ambiguous, not quiet


def test_signals_include_counts_in_description(monitor: IceMonitor) -> None:
    """Signal descriptions should embed the actual count so logs are self-explanatory."""
    _, signals = monitor._confidence_score(jumps=50, npc_kills=75, mining_signal=False)
    npc_signal = next(s for s in signals if "NPC" in s)
    jumps_signal = next(s for s in signals if "jumps" in s)
    assert "75" in npc_signal
    assert "50" in jumps_signal
