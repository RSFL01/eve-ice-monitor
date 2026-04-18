"""Tests for the Claude ambiguity-branch assessment in monitor.py."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ice_monitor.config import MonitorConfig
from ice_monitor.monitor import IceMonitor
from tests.conftest import FakeClaudeClient


@pytest.fixture
def monitor_with_claude(config: MonitorConfig) -> IceMonitor:
    """Monitor with a FakeClaudeClient attached."""
    return IceMonitor(system_name="Riavayed", config=config, claude=FakeClaudeClient())


# -------- _claude_assess --------

def test_assess_returns_active_when_claude_says_active(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "active", "reasoning": "npc kills trending up"})
    ]
    verdict = monitor_with_claude._claude_assess(
        jumps=20, npc_kills=20, mining_signal=False, current_qty=500
    )
    assert verdict == "active"


def test_assess_returns_cleared_when_claude_says_cleared(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "cleared", "reasoning": "belt looks quiet"})
    ]
    verdict = monitor_with_claude._claude_assess(
        jumps=5, npc_kills=10, mining_signal=False, current_qty=0
    )
    assert verdict == "cleared"


def test_assess_returns_ambiguous_when_claude_says_ambiguous(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "ambiguous", "reasoning": "mixed signals"})
    ]
    verdict = monitor_with_claude._claude_assess(
        jumps=15, npc_kills=15, mining_signal=False, current_qty=-1
    )
    assert verdict == "ambiguous"


def test_assess_normalizes_unexpected_verdict_to_ambiguous(
    monitor_with_claude: IceMonitor,
) -> None:
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "maybe", "reasoning": "???"})
    ]
    verdict = monitor_with_claude._claude_assess(
        jumps=15, npc_kills=15, mining_signal=False, current_qty=-1
    )
    assert verdict == "ambiguous"


def test_assess_returns_ambiguous_on_invalid_json(monitor_with_claude: IceMonitor) -> None:
    monitor_with_claude.claude.messages._responses = ["this is not json"]
    verdict = monitor_with_claude._claude_assess(
        jumps=15, npc_kills=15, mining_signal=False, current_qty=-1
    )
    assert verdict == "ambiguous"


def test_assess_returns_ambiguous_on_claude_exception(monitor_with_claude: IceMonitor) -> None:
    def raise_(**_: object) -> None:
        raise RuntimeError("API blew up")

    monitor_with_claude.claude.messages.create = raise_  # type: ignore[method-assign]
    verdict = monitor_with_claude._claude_assess(
        jumps=15, npc_kills=15, mining_signal=False, current_qty=-1
    )
    assert verdict == "ambiguous"


def test_assess_prompt_embeds_current_signals(monitor_with_claude: IceMonitor) -> None:
    """Sanity check that the prompt actually includes the signal counts."""
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "ambiguous", "reasoning": "x"})
    ]
    monitor_with_claude._claude_assess(
        jumps=42, npc_kills=17, mining_signal=True, current_qty=1234
    )
    call = monitor_with_claude.claude.messages.calls[0]
    prompt = call["messages"][0]["content"]
    assert "42" in prompt   # jumps
    assert "17" in prompt   # npc kills
    assert "1234" in prompt # current qty
    assert "yes" in prompt  # mining_signal reported as yes/no


# -------- run_once: ambiguous score routes through Claude --------

def _patch_esi_activity(monitor: IceMonitor, jumps: int, npc: int) -> None:
    """Make _get_system_activity return (jumps, npc, 0) without hitting the network."""
    def _fake() -> tuple[int, int, int]:
        return jumps, npc, 0
    monitor._get_system_activity = _fake  # type: ignore[method-assign]


def _patch_no_auth(monitor: IceMonitor) -> None:
    """Ensure run_once skips the authenticated mining fetch."""
    monitor._get_ice_quantity = lambda *a, **k: -1  # type: ignore[method-assign]


def test_run_once_ambiguous_active_verdict_activates_belt(
    monitor_with_claude: IceMonitor,
) -> None:
    # 35 jumps alone = score 20 (at CONFIDENCE_QUIET boundary, ambiguous range)
    _patch_esi_activity(monitor_with_claude, jumps=35, npc=0)
    _patch_no_auth(monitor_with_claude)

    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "active", "reasoning": "x"})
    ]
    # Skip real ESI token lookup
    with patch("ice_monitor.monitor.get_valid_token", return_value=None), \
         patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor_with_claude.run_once()

    assert monitor_with_claude.state.ice_belt_active is True
    alert.assert_called_once()
    # Assert the alert body mentions Claude-assist
    assert "Claude assist" in alert.call_args.args[2]


def test_run_once_ambiguous_cleared_verdict_increments_quiet_counter(
    monitor_with_claude: IceMonitor,
) -> None:
    # Seed belt as active & eligible for clearing
    from datetime import datetime, timedelta, timezone
    three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    monitor_with_claude.state.ice_belt_active = True
    monitor_with_claude.state.belt_active_since = three_hours_ago.isoformat()
    # One quiet poll shy of the threshold
    monitor_with_claude.state.no_change_polls = (
        monitor_with_claude.config.stale_polls_threshold - 1
    )

    _patch_esi_activity(monitor_with_claude, jumps=35, npc=0)
    _patch_no_auth(monitor_with_claude)
    monitor_with_claude.claude.messages._responses = [
        json.dumps({"verdict": "cleared", "reasoning": "quiet"})
    ]

    with patch("ice_monitor.monitor.get_valid_token", return_value=None), \
         patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor_with_claude.run_once()

    # Poll incremented → threshold reached → belt cleared
    assert monitor_with_claude.state.ice_belt_active is False
    alert.assert_called_once()


def test_run_once_ambiguous_no_claude_holds_state(monitor: IceMonitor) -> None:
    """Without Claude, ambiguous range holds state — belt stays inactive."""
    _patch_esi_activity(monitor, jumps=35, npc=0)  # score=20, ambiguous
    _patch_no_auth(monitor)

    with patch("ice_monitor.monitor.get_valid_token", return_value=None), \
         patch("ice_monitor.monitor.send_discord_alert") as alert:
        monitor.run_once()

    assert monitor.state.ice_belt_active is False
    alert.assert_not_called()
