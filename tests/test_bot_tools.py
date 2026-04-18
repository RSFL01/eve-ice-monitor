"""Tests for the Discord bot's local tool dispatch and conversation hygiene."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ice_monitor.bot import (
    _belt_status_summary,
    _execute_tool,
    _format_price_data,
    _handle_belt_down,
)
from ice_monitor.prices import MARKET_HUBS, TRACKED_ITEMS
from ice_monitor.state import MonitorState, save_state

# -------- _belt_status_summary --------

def test_belt_status_active(tmp_path: Path) -> None:
    state = MonitorState(ice_belt_active=True)
    save_state(tmp_path / "state.json", state)
    result = _belt_status_summary(tmp_path / "state.json")
    assert "ACTIVE" in result


def test_belt_status_down_with_future_respawn(tmp_path: Path) -> None:
    respawn = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
    state = MonitorState(ice_belt_active=False, estimated_respawn_time=respawn.isoformat())
    save_state(tmp_path / "state.json", state)
    result = _belt_status_summary(tmp_path / "state.json")
    assert "DOWN" in result
    assert "2h" in result


def test_belt_status_down_respawn_overdue(tmp_path: Path) -> None:
    respawn = datetime.now(timezone.utc) - timedelta(minutes=12)
    state = MonitorState(ice_belt_active=False, estimated_respawn_time=respawn.isoformat())
    save_state(tmp_path / "state.json", state)
    result = _belt_status_summary(tmp_path / "state.json")
    assert "OVERDUE" in result


def test_belt_status_unknown(tmp_path: Path) -> None:
    # No state file exists, no respawn time
    result = _belt_status_summary(tmp_path / "state.json")
    assert "unknown" in result.lower()


# -------- _handle_belt_down --------

def test_handle_belt_down_clears_belt_and_sets_respawn(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    active_since = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0)
    save_state(state_file, MonitorState(
        ice_belt_active=True,
        belt_active_since=active_since.isoformat(),
    ))

    result = _handle_belt_down(state_file, respawn_hours=6)

    from ice_monitor.state import load_state
    loaded = load_state(state_file)
    assert loaded.ice_belt_active is False
    assert loaded.belt_cleared_time is not None
    assert loaded.estimated_respawn_time is not None
    assert loaded.respawn_alert_sent is False
    # Duration recorded (~4 hours)
    assert len(loaded.belt_durations_hours) == 1
    assert 3.9 <= loaded.belt_durations_hours[0] <= 4.1
    # Response mentions system
    assert "Riavayed" in result


def test_handle_belt_down_without_active_since(tmp_path: Path) -> None:
    """Marking cleared without an active-since timestamp shouldn't record a duration."""
    state_file = tmp_path / "state.json"
    save_state(state_file, MonitorState(ice_belt_active=True, belt_active_since=None))
    _handle_belt_down(state_file, respawn_hours=6)
    from ice_monitor.state import load_state
    loaded = load_state(state_file)
    assert loaded.belt_durations_hours == []


# -------- _format_price_data --------

def test_format_price_data_includes_all_items() -> None:
    data = {
        "jita": {
            item: {"min": 100.0, "max": 200.0, "avg": 150.0, "trend_pct": 2.5}
            for item in TRACKED_ITEMS
        },
    }
    output = _format_price_data(data, ["jita"])
    assert "JITA:" in output
    for item in TRACKED_ITEMS:
        assert item in output
    assert "+2.5%" in output


def test_format_price_data_handles_no_data() -> None:
    data = {"jita": {item: None for item in TRACKED_ITEMS}}
    output = _format_price_data(data, ["jita"])
    assert "no data" in output


def test_format_price_data_handles_missing_min() -> None:
    data = {
        "jita": {
            TRACKED_ITEMS[0]: {"min": None, "max": None, "avg": None, "trend_pct": None},
            TRACKED_ITEMS[1]: None,
            TRACKED_ITEMS[2]: None,
        }
    }
    output = _format_price_data(data, ["jita"])
    # Both None-valued and None-summary entries collapse to "no data"
    assert output.count("no data") == 3


# -------- _execute_tool dispatch --------

async def test_execute_tool_get_belt_status(tmp_path: Path) -> None:
    save_state(tmp_path / "state.json", MonitorState(ice_belt_active=True))
    result = await _execute_tool("get_belt_status", {}, tmp_path / "state.json", respawn_hours=6)
    assert "ACTIVE" in result


async def test_execute_tool_mark_belt_cleared(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    save_state(state_file, MonitorState(ice_belt_active=True))
    result = await _execute_tool("mark_belt_cleared", {}, state_file, respawn_hours=6)
    from ice_monitor.state import load_state
    assert load_state(state_file).ice_belt_active is False
    assert "cleared" in result.lower()


async def test_execute_tool_get_duration_history_empty(tmp_path: Path) -> None:
    save_state(tmp_path / "state.json", MonitorState())
    result = await _execute_tool("get_duration_history", {}, tmp_path / "state.json", respawn_hours=6)
    assert "No belt duration history" in result


async def test_execute_tool_get_duration_history_with_data(tmp_path: Path) -> None:
    save_state(
        tmp_path / "state.json",
        MonitorState(belt_durations_hours=[4.0, 5.5, 3.25]),
    )
    result = await _execute_tool("get_duration_history", {}, tmp_path / "state.json", respawn_hours=6)
    assert "3 sessions" in result


async def test_execute_tool_unknown_returns_error(tmp_path: Path) -> None:
    result = await _execute_tool("does_not_exist", {}, tmp_path / "state.json", respawn_hours=6)
    assert "Unknown tool" in result


async def test_execute_tool_get_price_data(tmp_path: Path) -> None:
    fake = {
        hub: {item: {"min": 100.0, "max": 200.0, "avg": 150.0, "trend_pct": 1.0}
              for item in TRACKED_ITEMS}
        for hub in MARKET_HUBS
    }
    with patch("ice_monitor.bot.fetch_price_data", return_value=fake):
        result = await _execute_tool(
            "get_price_data", {"hub": "jita"}, tmp_path / "state.json", respawn_hours=6
        )
    assert "JITA:" in result
    assert "100" in result  # min price


async def test_execute_tool_get_price_data_all_hubs(tmp_path: Path) -> None:
    fake = {
        hub: {item: {"min": 100.0, "max": 200.0, "avg": 150.0, "trend_pct": 1.0}
              for item in TRACKED_ITEMS}
        for hub in MARKET_HUBS
    }
    with patch("ice_monitor.bot.fetch_price_data", return_value=fake):
        result = await _execute_tool(
            "get_price_data", {"hub": "all"}, tmp_path / "state.json", respawn_hours=6
        )
    for hub in MARKET_HUBS:
        assert hub.upper() in result


# -------- Orphaned tool_result pruning (bot.py:196-206) --------
#
# Not exposed as a function; logic is inline in _claude_agentic. Cover by
# simulating the prune algorithm directly on representative inputs.

def _prune_orphan_prefix(history: list) -> None:
    """Mirror of the inline prune loop in _claude_agentic."""
    while history:
        first = history[0]
        if first.get("role") != "user":
            history.pop(0)
            continue
        c = first.get("content", "")
        if isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "tool_result":
            history.pop(0)
            continue
        break


def test_prune_drops_leading_tool_result_user_message() -> None:
    history = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
        {"role": "user", "content": "hello"},
    ]
    _prune_orphan_prefix(history)
    assert len(history) == 1
    assert history[0]["content"] == "hello"


def test_prune_drops_leading_assistant_message() -> None:
    history = [
        {"role": "assistant", "content": "stale"},
        {"role": "user", "content": "hello"},
    ]
    _prune_orphan_prefix(history)
    assert len(history) == 1
    assert history[0]["role"] == "user"


def test_prune_keeps_clean_history_intact() -> None:
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    _prune_orphan_prefix(history)
    assert len(history) == 2


def test_prune_drops_all_leading_junk_until_clean_user() -> None:
    history = [
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "real message"},
    ]
    _prune_orphan_prefix(history)
    assert len(history) == 1
    assert history[0]["content"] == "real message"


def test_prune_empty_history() -> None:
    history = []
    _prune_orphan_prefix(history)  # must not crash
    assert history == []
