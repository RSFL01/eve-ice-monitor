from __future__ import annotations

from datetime import datetime, timezone
import logging

import requests


log = logging.getLogger("ice-monitor")



def send_discord_alert(webhook_url: str, title: str, message: str) -> None:
    if not webhook_url:
        log.info("Discord webhook not configured; message: %s", message)
        return

    payload = {
        "username": "EVE Ice Monitor",
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": 3447003,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code not in (200, 204):
            log.warning("Discord returned status %s: %s", response.status_code, response.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to send Discord notification: %s", exc)
