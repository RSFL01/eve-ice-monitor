from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class MonitorState:
    ice_belt_active: bool = False
    belt_cleared_time: str | None = None
    estimated_respawn_time: str | None = None
    respawn_alert_sent: bool = False
    last_ice_quantity: int = 0
    last_quantity_date: str | None = None
    no_change_polls: int = 0
    belt_active_since: str | None = None
    belt_durations_hours: list = field(default_factory=list)  # rolling history of belt durations



def load_state(path: Path) -> MonitorState:
    if not path.exists():
        return MonitorState()

    data = json.loads(path.read_text(encoding="utf-8"))
    valid = {f.name for f in fields(MonitorState)}
    return MonitorState(**{k: v for k, v in data.items() if k in valid})



def save_state(path: Path, state: MonitorState) -> None:
    payload: dict[str, Any] = asdict(state)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")



MAX_DURATION_HISTORY = 20  # keep last 20 belt sessions


def record_belt_duration(state: MonitorState, active_since_iso: str, cleared_at: datetime) -> None:
    """Append duration (hours) of the just-cleared belt to the rolling history."""
    try:
        active_since = datetime.fromisoformat(active_since_iso.replace("Z", "+00:00"))
        hours = (cleared_at - active_since).total_seconds() / 3600
        if hours > 0:
            state.belt_durations_hours.append(round(hours, 2))
            if len(state.belt_durations_hours) > MAX_DURATION_HISTORY:
                state.belt_durations_hours = state.belt_durations_hours[-MAX_DURATION_HISTORY:]
    except Exception:
        pass


def belt_duration_summary(state: MonitorState) -> str:
    """Return a human-readable summary of belt duration statistics."""
    durations = state.belt_durations_hours
    if not durations:
        return "No belt duration history recorded yet."
    avg = sum(durations) / len(durations)
    mn, mx = min(durations), max(durations)

    def fmt(h: float) -> str:
        hrs, mins = int(h), int((h % 1) * 60)
        return f"{hrs}h {mins}m" if hrs else f"{mins}m"

    return (
        f"Belt duration history ({len(durations)} session{'s' if len(durations) != 1 else ''}):\n"
        f"  Average: **{fmt(avg)}**\n"
        f"  Shortest: {fmt(mn)}\n"
        f"  Longest: {fmt(mx)}"
    )


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def format_time(dt: datetime) -> str:
    """Format a UTC datetime as UTC / EST / CST with DST-aware abbreviations."""
    from zoneinfo import ZoneInfo
    eastern = dt.astimezone(ZoneInfo("America/New_York"))
    central = dt.astimezone(ZoneInfo("America/Chicago"))
    east_abbr = eastern.strftime("%Z")
    cent_abbr = central.strftime("%Z")
    return (
        f"{dt.strftime('%Y-%m-%d %H:%M')} UTC | "
        f"{eastern.strftime('%H:%M')} {east_abbr} | "
        f"{central.strftime('%H:%M')} {cent_abbr}"
    )
