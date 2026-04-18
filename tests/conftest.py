"""Shared pytest fixtures for ice_monitor tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ice_monitor.config import MonitorConfig
from ice_monitor.monitor import IceMonitor


@pytest.fixture
def config(tmp_path: Path) -> MonitorConfig:
    """A MonitorConfig pointing at tmp_path for state + tokens."""
    return MonitorConfig(
        discord_webhook_url="",  # no-op — send_discord_alert is patched per-test
        respawn_hours=6,
        respawn_alert_minutes_before=15,
        stale_polls_threshold=10,
        min_active_hours=2,
        state_file=tmp_path / "state.json",
        esi_client_id="test-client-id",
        esi_client_secret="test-client-secret",
        esi_token_file=tmp_path / "tokens.json",
    )


@pytest.fixture
def monitor(config: MonitorConfig) -> IceMonitor:
    """An IceMonitor for Riavayed (hardcoded in KNOWN_SYSTEM_IDS, no network call)."""
    # System name "Riavayed" short-circuits via KNOWN_SYSTEM_IDS, no ESI request.
    return IceMonitor(system_name="Riavayed", config=config, claude=None)


class FakeClaudeResponse:
    """Mimics anthropic SDK response shape: .content[0].text"""

    def __init__(self, text: str) -> None:
        self.content = [type("Block", (), {"text": text})()]


class FakeClaudeMessages:
    """Mimics client.messages — records calls and returns canned responses."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeClaudeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            return FakeClaudeResponse("")
        return FakeClaudeResponse(self._responses.pop(0))


class FakeClaudeClient:
    """Drop-in replacement for anthropic.Anthropic in tests."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.messages = FakeClaudeMessages(responses)


@pytest.fixture
def fake_claude() -> FakeClaudeClient:
    """A FakeClaudeClient with no canned responses (configure per-test)."""
    return FakeClaudeClient()
