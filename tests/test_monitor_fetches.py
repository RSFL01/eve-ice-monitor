"""Tests for monitor's ESI fetch methods and Claude alert composition."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ice_monitor.config import MonitorConfig
from ice_monitor.monitor import IceMonitor
from tests.conftest import FakeClaudeClient


def _mock_response(data: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = data
    return resp


# -------- _get_system_activity --------

def test_get_system_activity_parses_our_system(monitor: IceMonitor) -> None:
    jumps_data = [
        {"system_id": 30002993, "ship_jumps": 42},
        {"system_id": 99999999, "ship_jumps": 100},  # some other system
    ]
    kills_data = [
        {"system_id": 30002993, "npc_kills": 55, "ship_kills": 3, "pod_kills": 1},
    ]

    def fake_get(url: str, timeout: int) -> MagicMock:
        if "jumps" in url:
            return _mock_response(jumps_data)
        return _mock_response(kills_data)

    with patch("ice_monitor.monitor.requests.get", side_effect=fake_get):
        jumps, npc, ships = monitor._get_system_activity()

    assert jumps == 42
    assert npc == 55
    assert ships == 3


def test_get_system_activity_defaults_to_zero_when_system_missing(
    monitor: IceMonitor,
) -> None:
    # Neither endpoint returns our system
    with patch("ice_monitor.monitor.requests.get", return_value=_mock_response([])):
        jumps, npc, ships = monitor._get_system_activity()
    assert (jumps, npc, ships) == (0, 0, 0)


def test_get_system_activity_returns_sentinel_on_failure(monitor: IceMonitor) -> None:
    with patch(
        "ice_monitor.monitor.requests.get",
        side_effect=RuntimeError("ESI 503"),
    ):
        jumps, npc, ships = monitor._get_system_activity()
    assert (jumps, npc, ships) == (-1, -1, -1)


def test_get_system_activity_handles_legacy_jumps_key(monitor: IceMonitor) -> None:
    """Older ESI payloads used 'jumps' instead of 'ship_jumps'."""
    jumps_data = [{"system_id": 30002993, "jumps": 17}]
    kills_data = [{"system_id": 30002993, "npc_kills": 0, "ship_kills": 0, "pod_kills": 0}]

    def fake_get(url: str, timeout: int) -> MagicMock:
        return _mock_response(jumps_data if "jumps" in url else kills_data)

    with patch("ice_monitor.monitor.requests.get", side_effect=fake_get):
        jumps, _, _ = monitor._get_system_activity()
    assert jumps == 17


# -------- _get_ice_quantity --------

def test_get_ice_quantity_sums_today_ice_in_system(monitor: IceMonitor) -> None:
    today = "2026-04-17"
    mining_data = [
        # Today, our system, ice ore → count
        {"date": today, "solar_system_id": 30002993, "type_id": 16262, "quantity": 1000},
        {"date": today, "solar_system_id": 30002993, "type_id": 16273, "quantity": 2000},
        # Today, our system, NOT ice ore (e.g. regular ore) → skip
        {"date": today, "solar_system_id": 30002993, "type_id": 1230, "quantity": 500},
        # Today, wrong system → skip
        {"date": today, "solar_system_id": 12345, "type_id": 16262, "quantity": 9999},
        # Wrong date → skip
        {"date": "2026-04-16", "solar_system_id": 30002993, "type_id": 16262, "quantity": 100},
    ]
    with patch("ice_monitor.monitor.requests.get", return_value=_mock_response(mining_data)):
        total = monitor._get_ice_quantity("access-token", character_id=999, today=today)
    assert total == 3000


def test_get_ice_quantity_empty_ledger(monitor: IceMonitor) -> None:
    with patch("ice_monitor.monitor.requests.get", return_value=_mock_response([])):
        total = monitor._get_ice_quantity("access-token", 999, "2026-04-17")
    assert total == 0


def test_get_ice_quantity_returns_sentinel_on_failure(monitor: IceMonitor) -> None:
    with patch(
        "ice_monitor.monitor.requests.get",
        side_effect=RuntimeError("401 Unauthorized"),
    ):
        total = monitor._get_ice_quantity("bad-token", 999, "2026-04-17")
    assert total == -1


def test_get_ice_quantity_sends_bearer_auth_header(monitor: IceMonitor) -> None:
    with patch(
        "ice_monitor.monitor.requests.get",
        return_value=_mock_response([]),
    ) as mock_get:
        monitor._get_ice_quantity("token-abc", 777, "2026-04-17")

    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer token-abc"


# -------- _claude_compose_alert --------

@pytest.fixture
def monitor_with_claude(config: MonitorConfig) -> IceMonitor:
    return IceMonitor(system_name="Riavayed", config=config, claude=FakeClaudeClient())


def test_compose_alert_no_claude_returns_empty(monitor: IceMonitor) -> None:
    """When Claude isn't configured, caller falls back to f-string templates."""
    assert monitor._claude_compose_alert("spawned", score=60) == ""


def test_compose_alert_spawned_includes_data_in_prompt(
    monitor_with_claude: IceMonitor,
) -> None:
    monitor_with_claude.claude.messages._responses = ["**Belt up.** Undock now."]
    result = monitor_with_claude._claude_compose_alert(
        "spawned",
        score=80,
        signals_str="NPC kills 60 (+40)",
        jumps=45,
        npc_kills=60,
        current_qty=1500,
        detected_fmt="2026-04-17 12:00 UTC",
    )
    assert "Belt up" in result
    prompt = monitor_with_claude.claude.messages.calls[0]["messages"][0]["content"]
    assert "80" in prompt
    assert "NPC kills 60 (+40)" in prompt
    assert "1500" in prompt


def test_compose_alert_cleared_template(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = ["Belt gone. Next in 6h."]
    result = monitor_with_claude._claude_compose_alert(
        "cleared",
        score=10,
        cleared_fmt="12:00 UTC",
        respawn_fmt="18:00 UTC",
    )
    assert result == "Belt gone. Next in 6h."


def test_compose_alert_respawn_warning_template(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = ["Respawn imminent!"]
    result = monitor_with_claude._claude_compose_alert(
        "respawn_warning",
        due_fmt="12:15 UTC",
        minutes_before=15,
    )
    assert result == "Respawn imminent!"


def test_compose_alert_unknown_event_returns_empty(
    monitor_with_claude: IceMonitor,
) -> None:
    assert monitor_with_claude._claude_compose_alert("unknown_event") == ""


def test_compose_alert_returns_empty_on_claude_exception(
    monitor_with_claude: IceMonitor,
) -> None:
    def raise_(**_: object) -> None:
        raise RuntimeError("API failure")
    monitor_with_claude.claude.messages.create = raise_  # type: ignore[method-assign]
    # Caller will fall back to f-string template
    assert monitor_with_claude._claude_compose_alert("spawned", score=70) == ""


# -------- _resolve_system_id --------

def test_resolve_known_system_skips_network(monitor: IceMonitor) -> None:
    # Re-resolving Riavayed should not hit the network (it's in KNOWN_SYSTEM_IDS)
    with patch("ice_monitor.monitor.requests.post") as mock_post:
        sid = monitor._resolve_system_id("Riavayed")
    assert sid == 30002993
    mock_post.assert_not_called()


def test_resolve_unknown_system_hits_esi(config: MonitorConfig) -> None:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"systems": [{"name": "Jita", "id": 30000142}]}

    with patch("ice_monitor.monitor.requests.post", return_value=resp):
        # Can't use the `monitor` fixture because it resolves system_id at __init__;
        # bypass that by patching before construction.
        m = IceMonitor(system_name="Jita", config=config)

    assert m.system_id == 30000142


def test_resolve_unknown_system_not_found_raises(config: MonitorConfig) -> None:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"systems": []}  # empty response

    with patch("ice_monitor.monitor.requests.post", return_value=resp):
        with pytest.raises(ValueError, match="not found"):
            IceMonitor(system_name="NotARealSystem", config=config)
