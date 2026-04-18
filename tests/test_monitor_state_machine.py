"""Tests for the ice-belt state machine: activate, clear, respawn warning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from ice_monitor.monitor import IceMonitor

# -------- _maybe_activate_belt --------

def test_activate_transitions_from_inactive(monitor: IceMonitor) -> None:
    assert monitor.state.ice_belt_active is False
    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor._maybe_activate_belt(
            score=80, signals=["NPC kills 60 (+40)", "mining ledger (+40)"],
            jumps=20, npc_kills=60, current_qty=1500,
        )
    assert monitor.state.ice_belt_active is True
    assert monitor.state.belt_active_since is not None
    assert monitor.state.no_change_polls == 0
    alert.assert_called_once()
    # Alert title includes confidence score
    args = alert.call_args.args
    assert "80/100" in args[1]


def test_activate_is_idempotent_when_already_active(monitor: IceMonitor) -> None:
    """Calling activate on an already-active belt should NOT re-send the alert."""
    monitor.state.ice_belt_active = True
    monitor.state.belt_active_since = "2026-01-01T10:00:00+00:00"
    monitor.state.no_change_polls = 7  # nonzero — should still reset

    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor._maybe_activate_belt(
            score=85, signals=["mining ledger (+40)"],
            jumps=50, npc_kills=40, current_qty=2000,
        )
    # Still active, no new alert
    assert monitor.state.ice_belt_active is True
    # no_change_polls should be reset regardless (a strong signal is a strong signal)
    assert monitor.state.no_change_polls == 0
    alert.assert_not_called()
    # Active-since unchanged
    assert monitor.state.belt_active_since == "2026-01-01T10:00:00+00:00"


def test_activate_flags_claude_assist_in_signals(monitor: IceMonitor) -> None:
    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor._maybe_activate_belt(
            score=50, signals=["jumps 40 (+20)"],
            jumps=40, npc_kills=10, current_qty=0,
            claude_assisted=True,
        )
    body = alert.call_args.args[2]
    assert "Claude assist" in body


# -------- _maybe_clear_belt --------

@pytest.fixture
def active_monitor(monitor: IceMonitor) -> IceMonitor:
    """Monitor with belt that's been active for 3 hours (past min_active_hours=2)."""
    three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    monitor.state.ice_belt_active = True
    monitor.state.belt_active_since = three_hours_ago.isoformat()
    return monitor


def test_clear_requires_stale_polls_threshold(active_monitor: IceMonitor) -> None:
    active_monitor.state.no_change_polls = active_monitor.config.stale_polls_threshold - 1
    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        active_monitor._maybe_clear_belt(score=10)
    assert active_monitor.state.ice_belt_active is True  # still active
    alert.assert_not_called()


def test_clear_fires_at_stale_polls_threshold(active_monitor: IceMonitor) -> None:
    active_monitor.state.no_change_polls = active_monitor.config.stale_polls_threshold
    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        active_monitor._maybe_clear_belt(score=10)
    assert active_monitor.state.ice_belt_active is False
    assert active_monitor.state.belt_cleared_time is not None
    assert active_monitor.state.estimated_respawn_time is not None
    assert active_monitor.state.respawn_alert_sent is False  # reset for next cycle
    assert active_monitor.state.last_ice_quantity == 0       # reset
    assert active_monitor.state.no_change_polls == 0
    alert.assert_called_once()


def test_clear_respects_min_active_hours(monitor: IceMonitor) -> None:
    """Even with stale polls threshold reached, don't clear before min_active_hours."""
    # Belt has only been active for 30 minutes (below min_active_hours=2)
    thirty_min_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    monitor.state.ice_belt_active = True
    monitor.state.belt_active_since = thirty_min_ago.isoformat()
    monitor.state.no_change_polls = monitor.config.stale_polls_threshold

    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor._maybe_clear_belt(score=5)
    assert monitor.state.ice_belt_active is True  # still held active
    alert.assert_not_called()


def test_clear_noop_when_belt_already_inactive(monitor: IceMonitor) -> None:
    monitor.state.ice_belt_active = False
    monitor.state.no_change_polls = 999
    with patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor._maybe_clear_belt(score=0)
    alert.assert_not_called()


def test_clear_records_belt_duration(active_monitor: IceMonitor) -> None:
    active_monitor.state.no_change_polls = active_monitor.config.stale_polls_threshold
    assert active_monitor.state.belt_durations_hours == []
    with patch("ice_monitor.monitor.send_discord_alert"):
        active_monitor._maybe_clear_belt(score=5)
    assert len(active_monitor.state.belt_durations_hours) == 1
    # Belt was active for ~3 hours
    assert 2.9 <= active_monitor.state.belt_durations_hours[0] <= 3.1


def test_clear_sets_respawn_to_now_plus_configured_hours(monitor: IceMonitor) -> None:
    # Must frame belt_active_since relative to the FROZEN clock, not real time.
    with freeze_time("2026-04-17 12:00:00"):
        monitor.state.ice_belt_active = True
        monitor.state.belt_active_since = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat()
        monitor.state.no_change_polls = monitor.config.stale_polls_threshold
        with patch("ice_monitor.monitor.send_discord_alert"):
            monitor._maybe_clear_belt(score=5)

    respawn = datetime.fromisoformat(monitor.state.estimated_respawn_time)
    # respawn_hours defaults to 6 in fixture → 12:00 + 6h = 18:00
    expected = datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc)
    assert abs((respawn - expected).total_seconds()) < 5


# -------- _should_send_respawn_warning --------

def test_no_warning_when_no_respawn_time(monitor: IceMonitor) -> None:
    monitor.state.estimated_respawn_time = None
    assert monitor._should_send_respawn_warning() is False


def test_no_warning_when_already_sent(monitor: IceMonitor) -> None:
    monitor.state.estimated_respawn_time = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    monitor.state.respawn_alert_sent = True
    assert monitor._should_send_respawn_warning() is False


def test_warning_fires_at_configured_window(monitor: IceMonitor) -> None:
    """Warning fires when current time is >= (respawn - respawn_alert_minutes_before)."""
    now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    # respawn in exactly 15 min (the configured window)
    respawn = now + timedelta(minutes=monitor.config.respawn_alert_minutes_before)
    monitor.state.estimated_respawn_time = respawn.isoformat()
    monitor.state.respawn_alert_sent = False

    with freeze_time(now):
        assert monitor._should_send_respawn_warning() is True


def test_warning_silent_before_window(monitor: IceMonitor) -> None:
    now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    # respawn in 1 hour (well outside 15-min window)
    respawn = now + timedelta(hours=1)
    monitor.state.estimated_respawn_time = respawn.isoformat()
    monitor.state.respawn_alert_sent = False

    with freeze_time(now):
        assert monitor._should_send_respawn_warning() is False


def test_warning_fires_even_after_respawn_time_passed(monitor: IceMonitor) -> None:
    """If the bot was down during the warning window, fire on next poll (still not sent)."""
    now = datetime(2026, 4, 17, 13, 0, tzinfo=timezone.utc)
    # respawn was 5 min ago
    respawn = now - timedelta(minutes=5)
    monitor.state.estimated_respawn_time = respawn.isoformat()
    monitor.state.respawn_alert_sent = False

    with freeze_time(now):
        assert monitor._should_send_respawn_warning() is True
