from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import requests

from .auth import get_valid_token, load_tokens
from .config import MonitorConfig
from .discord import send_discord_alert
from .state import format_time, load_state, record_belt_duration, save_state, utc_now_iso


ESI_BASE = "https://esi.evetech.net/latest"
log = logging.getLogger("ice-monitor")

# Known system name → ID mapping
KNOWN_SYSTEM_IDS: dict[str, int] = {
    "Riavayed": 30002993,
}

# EVE type IDs for all ice ores
ICE_ORE_TYPE_IDS = {
    16262, 16263, 16264, 16265, 16266, 16267,
    16268, 16269, 16270, 16271, 16272, 16273,
}

# npc_kills/hour threshold — above this indicates active belt mining
ACTIVITY_THRESHOLD = 30


class IceMonitor:
    def __init__(self, system_name: str, config: MonitorConfig) -> None:
        self.system_name = system_name
        self.config = config
        self.system_id = self._resolve_system_id(system_name)
        self.state = load_state(config.state_file)

    def _resolve_system_id(self, system_name: str) -> int:
        if system_name in KNOWN_SYSTEM_IDS:
            return KNOWN_SYSTEM_IDS[system_name]
        response = requests.post(
            f"{ESI_BASE}/universe/ids/",
            json=[system_name],
            timeout=15,
        )
        response.raise_for_status()
        systems = response.json().get("systems", [])
        if not systems:
            raise ValueError(f"System '{system_name}' not found")
        return int(systems[0]["id"])

    def _get_system_activity(self) -> tuple[int, int, int]:
        """Return (jumps, npc_kills, ship_kills) for target system. Returns (-1,-1,-1) on failure."""
        try:
            jumps_resp = requests.get(f"{ESI_BASE}/universe/system_jumps/", timeout=15)
            kills_resp = requests.get(f"{ESI_BASE}/universe/system_kills/", timeout=15)
            jumps_resp.raise_for_status()
            kills_resp.raise_for_status()
            jumps = next(
                (e.get("ship_jumps", e.get("jumps", 0)) for e in jumps_resp.json() if e.get("system_id") == self.system_id), 0
            )
            kills = next(
                (e for e in kills_resp.json() if e["system_id"] == self.system_id), {}
            )
            return jumps, kills.get("npc_kills", 0), kills.get("ship_kills", 0)
        except Exception as exc:
            log.warning("Activity fetch failed: %s", exc)
            return -1, -1, -1

    def _get_ice_quantity(self, access_token: str, character_id: int, today: str) -> int:
        """Return total ice ore units mined in target system today. Returns -1 on fetch failure."""
        total = 0
        try:
            resp = requests.get(
                f"{ESI_BASE}/characters/{character_id}/mining/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            for entry in resp.json():
                if (
                    entry.get("date") == today
                    and entry.get("solar_system_id") == self.system_id
                    and entry.get("type_id") in ICE_ORE_TYPE_IDS
                ):
                    total += int(entry.get("quantity", 0))
        except Exception as exc:
            log.warning("Mining ledger fetch failed: %s", exc)
            return -1
        return total

    def _should_send_respawn_warning(self) -> bool:
        if not self.state.estimated_respawn_time or self.state.respawn_alert_sent:
            return False
        due_at = datetime.fromisoformat(self.state.estimated_respawn_time.replace("Z", "+00:00"))
        warning_at = due_at - timedelta(minutes=self.config.respawn_alert_minutes_before)
        return datetime.now(timezone.utc) >= warning_at

    def run_once(self) -> None:
        today = date.today().isoformat()

        # Reset mining quantity tracking on UTC date rollover
        if self.state.last_quantity_date != today:
            self.state.last_ice_quantity = 0
            self.state.last_quantity_date = today

        # --- Activity heuristic (public ESI) ---
        jumps, npc_kills, ship_kills = self._get_system_activity()
        activity_ok = jumps >= 0
        activity_signal = activity_ok and npc_kills >= ACTIVITY_THRESHOLD

        # --- Mining ledger (authenticated ESI) ---
        current_qty = -1
        access_token = get_valid_token(
            self.config.esi_client_id, self.config.esi_client_secret, self.config.esi_token_file
        )
        if access_token:
            tokens = load_tokens(self.config.esi_token_file)
            current_qty = self._get_ice_quantity(access_token, tokens.character_id, today)

        mining_signal = current_qty > self.state.last_ice_quantity

        log.info(
            "System %s (%s): jumps=%s npc_kills=%s ship_kills=%s | ice_qty=%s last_qty=%s",
            self.system_name,
            self.system_id,
            jumps, npc_kills, ship_kills,
            current_qty,
            self.state.last_ice_quantity,
        )

        if not activity_ok:
            # Public fetch failed — skip state changes
            save_state(self.config.state_file, self.state)
            return

        # Update mining quantity if valid
        if current_qty >= 0:
            self.state.last_ice_quantity = current_qty

        if self._should_send_respawn_warning():
            due_at = datetime.fromisoformat(self.state.estimated_respawn_time.replace("Z", "+00:00"))
            send_discord_alert(
                self.config.discord_webhook_url,
                "Ice respawn window approaching",
                (
                    f"{self.system_name}: respawn expected within "
                    f"{self.config.respawn_alert_minutes_before} minutes.\n"
                    f"**ETA:** {format_time(due_at)}"
                ),
            )
            self.state.respawn_alert_sent = True

        belt_active = activity_signal or mining_signal

        if belt_active:
            self.state.no_change_polls = 0
            if not self.state.ice_belt_active:
                now = datetime.now(timezone.utc)
                self.state.ice_belt_active = True
                self.state.belt_active_since = now.isoformat()
                source = "mining ledger" if mining_signal else "activity"
                send_discord_alert(
                    self.config.discord_webhook_url,
                    "Ice belt confirmed",
                    (
                        f"{self.system_name}: belt active ({source}).\n"
                        f"**Detected:** {format_time(now)}\n"
                        f"**Jumps:** {jumps} | **NPC kills:** {npc_kills} | **Ice mined:** {current_qty if current_qty >= 0 else 'N/A'}"
                    ),
                )
        else:
            self.state.no_change_polls += 1
            if self.state.ice_belt_active and self.state.no_change_polls >= self.config.stale_polls_threshold:
                active_since = (
                    datetime.fromisoformat(self.state.belt_active_since)
                    if self.state.belt_active_since
                    else None
                )
                min_elapsed = (
                    active_since is not None
                    and (datetime.now(timezone.utc) - active_since) >= timedelta(hours=self.config.min_active_hours)
                )
                if min_elapsed:
                    cleared = datetime.now(timezone.utc)
                    respawn = cleared + timedelta(hours=self.config.respawn_hours)
                    if self.state.belt_active_since:
                        record_belt_duration(self.state, self.state.belt_active_since, cleared)
                    self.state.ice_belt_active = False
                    self.state.belt_active_since = None
                    self.state.belt_cleared_time = utc_now_iso()
                    self.state.estimated_respawn_time = respawn.isoformat()
                    self.state.respawn_alert_sent = False
                    self.state.last_ice_quantity = 0
                    self.state.no_change_polls = 0
                    send_discord_alert(
                        self.config.discord_webhook_url,
                        "Ice belt cleared",
                        (
                            f"{self.system_name}: both signals quiet — belt cleared.\n"
                            f"**Cleared:** {format_time(cleared)}\n"
                            f"**Next spawn:** {format_time(respawn)}"
                        ),
                    )
                else:
                    log.info("Quiet signals but min active time not met — holding belt active")

        save_state(self.config.state_file, self.state)
