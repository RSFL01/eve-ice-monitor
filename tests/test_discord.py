"""Tests for the Discord webhook notifier."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from ice_monitor.discord import send_discord_alert


def test_send_discord_alert_skips_when_webhook_empty() -> None:
    """Empty webhook URL is a no-op (logs only, never calls requests)."""
    with patch("ice_monitor.discord.requests.post") as post:
        send_discord_alert("", "title", "message")
    post.assert_not_called()


def test_send_discord_alert_posts_embed_payload() -> None:
    """A configured webhook gets a Discord-embed-shaped POST."""
    fake_response = MagicMock(status_code=204, text="")
    with patch("ice_monitor.discord.requests.post", return_value=fake_response) as post:
        send_discord_alert("https://discord.example/webhook", "Belt Active", "Ice spotted")

    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["timeout"] == 15
    payload = kwargs["json"]
    assert payload["username"] == "EVE Ice Monitor"
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "Belt Active"
    assert embed["description"] == "Ice spotted"
    assert "timestamp" in embed
    assert isinstance(embed["color"], int)


def test_send_discord_alert_logs_non_2xx_without_raising() -> None:
    """A 500 from Discord should be logged and swallowed."""
    fake_response = MagicMock(status_code=500, text="server error")
    with patch("ice_monitor.discord.requests.post", return_value=fake_response):
        # Must not raise
        send_discord_alert("https://discord.example/webhook", "t", "m")


def test_send_discord_alert_swallows_network_exceptions() -> None:
    """A requests exception should be caught and logged, never propagated."""
    with patch("ice_monitor.discord.requests.post", side_effect=RuntimeError("boom")):
        # Must not raise
        send_discord_alert("https://discord.example/webhook", "t", "m")
