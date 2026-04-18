from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MonitorConfig:
    discord_webhook_url: str
    respawn_hours: int
    respawn_alert_minutes_before: int
    stale_polls_threshold: int
    min_active_hours: int
    state_file: Path
    esi_client_id: str
    esi_client_secret: str
    esi_token_file: Path



def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw}") from exc



def load_config() -> MonitorConfig:
    return MonitorConfig(
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        respawn_hours=_get_int("ICE_RESPAWN_HOURS", 6),
        respawn_alert_minutes_before=_get_int("RESPAWN_ALERT_MINUTES_BEFORE", 15),
        stale_polls_threshold=_get_int("ICE_STALE_POLLS", 10),
        min_active_hours=_get_int("ICE_MIN_ACTIVE_HOURS", 2),
        state_file=Path(os.getenv("ICE_STATE_FILE", "ice_monitor_state.json")).expanduser(),
        esi_client_id=os.getenv("ESI_CLIENT_ID", "").strip(),
        esi_client_secret=os.getenv("ESI_CLIENT_SECRET", "").strip(),
        esi_token_file=Path(os.getenv("ESI_TOKEN_FILE", "esi_tokens.json")).expanduser(),
    )
