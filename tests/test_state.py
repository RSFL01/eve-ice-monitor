from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ice_monitor.state import (
    MAX_DURATION_HISTORY,
    MonitorState,
    belt_duration_summary,
    load_state,
    record_belt_duration,
    save_state,
    utc_now_iso,
)


def test_load_missing_state_returns_defaults(tmp_path: Path) -> None:
    state = load_state(tmp_path / "does-not-exist.json")
    assert state.ice_belt_active is False
    assert state.belt_cleared_time is None
    assert state.estimated_respawn_time is None
    assert state.respawn_alert_sent is False
    assert state.last_ice_quantity == 0
    assert state.last_quantity_date is None
    assert state.no_change_polls == 0
    assert state.belt_active_since is None
    assert state.belt_durations_hours == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    original = MonitorState(
        ice_belt_active=True,
        belt_active_since="2026-01-01T12:00:00+00:00",
        no_change_polls=3,
        last_ice_quantity=5000,
        belt_durations_hours=[4.5, 6.1],
    )
    save_state(state_file, original)

    loaded = load_state(state_file)
    assert loaded.ice_belt_active is True
    assert loaded.belt_active_since == "2026-01-01T12:00:00+00:00"
    assert loaded.no_change_polls == 3
    assert loaded.last_ice_quantity == 5000
    assert loaded.belt_durations_hours == [4.5, 6.1]


def test_load_ignores_unknown_fields(tmp_path: Path) -> None:
    """Forward compatibility: old state files with removed fields should still load."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        '{"ice_belt_active": true, "legacy_field": 999, "another_unknown": "x"}',
        encoding="utf-8",
    )
    loaded = load_state(state_file)
    assert loaded.ice_belt_active is True
    # Unknown fields silently dropped, defaults preserved
    assert loaded.no_change_polls == 0


def test_record_belt_duration_appends_hours() -> None:
    state = MonitorState()
    active_since = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    cleared_at = active_since + timedelta(hours=5, minutes=30)
    record_belt_duration(state, active_since.isoformat(), cleared_at)
    assert state.belt_durations_hours == [5.5]


def test_record_belt_duration_caps_history() -> None:
    state = MonitorState(belt_durations_hours=[1.0] * MAX_DURATION_HISTORY)
    active_since = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    cleared_at = active_since + timedelta(hours=7)
    record_belt_duration(state, active_since.isoformat(), cleared_at)
    assert len(state.belt_durations_hours) == MAX_DURATION_HISTORY
    assert state.belt_durations_hours[-1] == 7.0
    # Oldest entry (1.0) was dropped
    assert state.belt_durations_hours[0] == 1.0  # still 1.0 — we dropped one from the front


def test_record_belt_duration_ignores_zero_or_negative() -> None:
    state = MonitorState()
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    # cleared before active
    record_belt_duration(state, now.isoformat(), now - timedelta(hours=1))
    assert state.belt_durations_hours == []


def test_record_belt_duration_swallows_bad_timestamp() -> None:
    state = MonitorState()
    record_belt_duration(state, "not-a-timestamp", datetime.now(timezone.utc))
    assert state.belt_durations_hours == []


def test_belt_duration_summary_empty() -> None:
    assert belt_duration_summary(MonitorState()) == "No belt duration history recorded yet."


def test_belt_duration_summary_formats_stats() -> None:
    state = MonitorState(belt_durations_hours=[2.0, 4.0, 6.0])
    summary = belt_duration_summary(state)
    assert "3 sessions" in summary
    assert "Average" in summary
    assert "Shortest" in summary
    assert "Longest" in summary
    # Average is 4.0 → "4h 0m"
    assert "4h 0m" in summary


def test_belt_duration_summary_singular_session() -> None:
    state = MonitorState(belt_durations_hours=[3.25])
    summary = belt_duration_summary(state)
    assert "1 session" in summary
    assert "1 sessions" not in summary


def test_utc_now_iso_format() -> None:
    iso = utc_now_iso()
    assert iso.endswith("Z")
    # Parses as ISO without microseconds
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert parsed.microsecond == 0
