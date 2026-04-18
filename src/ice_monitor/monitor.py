from __future__ import annotations

import json as _json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import requests

from .auth import get_valid_token, load_tokens
from .config import MonitorConfig
from .discord import send_discord_alert
from .state import format_time, load_state, record_belt_duration, save_state, utc_now_iso

if TYPE_CHECKING:
    import anthropic


ESI_BASE = "https://esi.evetech.net/latest"
log = logging.getLogger("ice-monitor")

KNOWN_SYSTEM_IDS: dict[str, int] = {
    "Riavayed": 30002993,
}

ICE_ORE_TYPE_IDS = {
    16262, 16263, 16264, 16265, 16266, 16267,
    16268, 16269, 16270, 16271, 16272, 16273,
}

# Confidence scoring weights (total possible = 100)
SCORE_NPC_KILLS   = 40   # npc_kills >= NPC_KILLS_THRESHOLD
SCORE_MINING      = 40   # mining ledger quantity increased
SCORE_JUMPS       = 20   # jumps >= JUMPS_THRESHOLD

NPC_KILLS_THRESHOLD = 30
JUMPS_THRESHOLD     = 35
CONFIDENCE_ACTIVE   = 60   # score >= this → belt is active
CONFIDENCE_QUIET    = 20   # score < this → counts as a "quiet" poll toward clearing


class IceMonitor:
    def __init__(self, system_name: str, config: MonitorConfig, claude: anthropic.Anthropic | None = None) -> None:
        self.system_name = system_name
        self.config = config
        self.claude = claude
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
        """Return (jumps, npc_kills, ship_kills). Returns (-1,-1,-1) on failure."""
        try:
            jumps_resp = requests.get(f"{ESI_BASE}/universe/system_jumps/", timeout=15)
            kills_resp = requests.get(f"{ESI_BASE}/universe/system_kills/", timeout=15)
            jumps_resp.raise_for_status()
            kills_resp.raise_for_status()
            jumps = next(
                (e.get("ship_jumps", e.get("jumps", 0)) for e in jumps_resp.json() if e.get("system_id") == self.system_id), 0
            )
            kills: dict[str, Any] = next(
                (e for e in kills_resp.json() if e["system_id"] == self.system_id), {}
            )
            return jumps, kills.get("npc_kills", 0), kills.get("ship_kills", 0)
        except Exception as exc:
            log.warning("Activity fetch failed: %s", exc)
            return -1, -1, -1

    def _get_ice_quantity(self, access_token: str, character_id: int, today: str) -> int:
        """Return total ice ore units mined today. Returns -1 on failure."""
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

    def _confidence_score(self, jumps: int, npc_kills: int, mining_signal: bool) -> tuple[int, list[str]]:
        """Return (score 0-100, list of contributing signals)."""
        score = 0
        signals = []
        if npc_kills >= NPC_KILLS_THRESHOLD:
            score += SCORE_NPC_KILLS
            signals.append(f"NPC kills {npc_kills} (+{SCORE_NPC_KILLS})")
        if mining_signal:
            score += SCORE_MINING
            signals.append(f"mining ledger (+{SCORE_MINING})")
        if jumps >= JUMPS_THRESHOLD:
            score += SCORE_JUMPS
            signals.append(f"jumps {jumps} (+{SCORE_JUMPS})")
        return score, signals

    def _should_send_respawn_warning(self) -> bool:
        if not self.state.estimated_respawn_time or self.state.respawn_alert_sent:
            return False
        due_at = datetime.fromisoformat(self.state.estimated_respawn_time.replace("Z", "+00:00"))
        warning_at = due_at - timedelta(minutes=self.config.respawn_alert_minutes_before)
        return datetime.now(timezone.utc) >= warning_at

    def _maybe_activate_belt(
        self,
        score: int,
        signals: list[str],
        jumps: int,
        npc_kills: int,
        current_qty: int,
        *,
        claude_assisted: bool = False,
    ) -> None:
        self.state.no_change_polls = 0
        if not self.state.ice_belt_active:
            now = datetime.now(timezone.utc)
            self.state.ice_belt_active = True
            self.state.belt_active_since = now.isoformat()
            signals_str = ", ".join(signals)
            if claude_assisted:
                signals_str += " [Claude assist]"
            body = self._claude_compose_alert(
                "spawned",
                score=score,
                signals_str=signals_str,
                jumps=jumps,
                npc_kills=npc_kills,
                current_qty=current_qty,
                detected_fmt=format_time(now),
            ) or (
                f"{self.system_name}: belt active.\n"
                f"**Detected:** {format_time(now)}\n"
                f"**Signals:** {signals_str}\n"
                f"**Jumps:** {jumps} | **NPC kills:** {npc_kills} | "
                f"**Ice mined:** {current_qty if current_qty >= 0 else 'N/A'}"
            )
            send_discord_alert(
                self.config.discord_webhook_url,
                f"Ice belt confirmed — {score}/100 confidence",
                body,
            )

    def _maybe_clear_belt(self, score: int) -> None:
        if not self.state.ice_belt_active or self.state.no_change_polls < self.config.stale_polls_threshold:
            return
        active_since = (
            datetime.fromisoformat(self.state.belt_active_since)
            if self.state.belt_active_since else None
        )
        now = datetime.now(timezone.utc)
        min_elapsed = (
            active_since is not None
            and (now - active_since) >= timedelta(hours=self.config.min_active_hours)
        )
        if not min_elapsed:
            log.info("Low confidence (%s/100) but min active time not met — holding belt active", score)
            return
        respawn = now + timedelta(hours=self.config.respawn_hours)
        if self.state.belt_active_since:
            record_belt_duration(self.state, self.state.belt_active_since, now)
        self.state.ice_belt_active = False
        self.state.belt_active_since = None
        self.state.belt_cleared_time = utc_now_iso()
        self.state.estimated_respawn_time = respawn.isoformat()
        self.state.respawn_alert_sent = False
        self.state.last_ice_quantity = 0
        self.state.no_change_polls = 0
        body = self._claude_compose_alert(
            "cleared",
            score=score,
            cleared_fmt=format_time(now),
            respawn_fmt=format_time(respawn),
        ) or (
            f"{self.system_name}: confidence dropped to {score}/100 — belt cleared.\n"
            f"**Cleared:** {format_time(now)}\n"
            f"**Next spawn:** {format_time(respawn)}"
        )
        send_discord_alert(self.config.discord_webhook_url, "Ice belt cleared", body)

    def _claude_assess(self, jumps: int, npc_kills: int, mining_signal: bool, current_qty: int) -> str:
        """Ask Claude to assess belt state from ambiguous signals. Returns 'active', 'cleared', or 'ambiguous'."""
        prompt = (
            f"EVE Online — Riavayed (30002993) ice belt signals (last ~1 hour):\n"
            f"- Ship jumps: {jumps} (active threshold: {JUMPS_THRESHOLD}+)\n"
            f"- NPC kills: {npc_kills} (active threshold: {NPC_KILLS_THRESHOLD}+)\n"
            f"- Mining ledger increased: {'yes' if mining_signal else 'no'}"
            + (f" (total today: {current_qty})" if current_qty >= 0 else "") + "\n"
            f"- Current recorded state: {'ACTIVE' if self.state.ice_belt_active else 'DOWN'}\n\n"
            "These signals are in an ambiguous range (not clearly active or quiet). "
            "Based on EVE ice belt mechanics (belt attracts miners and NPCs when active, ~6h cycle), "
            "what is your assessment?\n"
            'Respond only with JSON: {"verdict": "active|cleared|ambiguous", "reasoning": "one sentence"}'
        )
        try:
            resp = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system="You assess EVE Online ice belt activity from ESI signals. Be decisive but accurate.",
                messages=[{"role": "user", "content": prompt}],
            )
            data = _json.loads(resp.content[0].text.strip())
            verdict = data.get("verdict", "ambiguous")
            log.info("Claude assess: %s — %s", verdict, data.get("reasoning", ""))
            return verdict if verdict in ("active", "cleared", "ambiguous") else "ambiguous"
        except Exception as exc:
            log.warning("Claude assess failed: %s", exc)
            return "ambiguous"

    def _claude_compose_alert(self, event: str, **kwargs: object) -> str:
        """Generate a Claude-written alert body. Returns '' on failure (caller uses f-string fallback)."""
        if not self.claude:
            return ""
        prompts: dict[str, str] = {
            "spawned": (
                f"Write a 2-3 line Discord alert body: ice belt just spawned in **{self.system_name}**.\n"
                f"Data: confidence={kwargs.get('score')}/100, signals={kwargs.get('signals_str')}, "
                f"jumps={kwargs.get('jumps')}, NPC kills={kwargs.get('npc_kills')}, "
                f"ice mined today={kwargs.get('current_qty', 'N/A')}, detected={kwargs.get('detected_fmt')}\n"
                "Use Discord bold markdown. Tell miners to undock. Be direct."
            ),
            "cleared": (
                f"Write a 2-3 line Discord alert body: ice belt cleared in **{self.system_name}**.\n"
                f"Data: confidence={kwargs.get('score')}/100, cleared={kwargs.get('cleared_fmt')}, "
                f"next spawn ~{kwargs.get('respawn_fmt')}\n"
                "Use Discord bold markdown. Mention prep time before next spawn."
            ),
            "respawn_warning": (
                f"Write a 2-3 line Discord alert body: ice belt respawn imminent in **{self.system_name}**.\n"
                f"Data: ETA={kwargs.get('due_fmt')}, ~{kwargs.get('minutes_before')} minutes away.\n"
                "Use Discord bold markdown. Urgent tone — get to system now."
            ),
        }
        prompt = prompts.get(event)
        if not prompt:
            return ""
        try:
            resp = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system="Write concise EVE Online Discord alert bodies. Bold key facts. No emojis. 2-3 lines.",
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            log.warning("Claude compose alert failed (%s): %s", event, exc)
            return ""

    def run_once(self) -> None:
        today = date.today().isoformat()

        if self.state.last_quantity_date != today:
            self.state.last_ice_quantity = 0
            self.state.last_quantity_date = today

        # --- Public ESI signals ---
        jumps, npc_kills, ship_kills = self._get_system_activity()
        if jumps < 0:
            log.warning("Activity fetch failed — skipping poll")
            save_state(self.config.state_file, self.state)
            return

        # --- Mining ledger ---
        current_qty = -1
        access_token = get_valid_token(
            self.config.esi_client_id, self.config.esi_client_secret, self.config.esi_token_file
        )
        if access_token:
            tokens = load_tokens(self.config.esi_token_file)
            current_qty = self._get_ice_quantity(access_token, tokens.character_id, today)

        mining_signal = current_qty > self.state.last_ice_quantity
        if current_qty >= 0:
            self.state.last_ice_quantity = current_qty

        # --- Confidence score ---
        score, signals = self._confidence_score(jumps, npc_kills, mining_signal)

        log.info(
            "System %s (%s): jumps=%s npc_kills=%s ship_kills=%s | ice_qty=%s | confidence=%s/100 [%s]",
            self.system_name, self.system_id,
            jumps, npc_kills, ship_kills,
            current_qty if current_qty >= 0 else "N/A",
            score,
            ", ".join(signals) if signals else "no signal",
        )

        if self._should_send_respawn_warning():
            due_at = datetime.fromisoformat(self.state.estimated_respawn_time.replace("Z", "+00:00"))
            body = self._claude_compose_alert(
                "respawn_warning",
                due_fmt=format_time(due_at),
                minutes_before=self.config.respawn_alert_minutes_before,
            ) or (
                f"{self.system_name}: respawn expected within "
                f"{self.config.respawn_alert_minutes_before} minutes.\n"
                f"**ETA:** {format_time(due_at)}"
            )
            send_discord_alert(self.config.discord_webhook_url, "Ice respawn window approaching", body)
            self.state.respawn_alert_sent = True

        # --- Belt active ---
        if score >= CONFIDENCE_ACTIVE:
            self._maybe_activate_belt(score, signals, jumps, npc_kills, current_qty)

        # --- Belt quiet ---
        elif score < CONFIDENCE_QUIET:
            self.state.no_change_polls += 1
            self._maybe_clear_belt(score)

        # --- Ambiguous (20–59): ask Claude if available, else hold ---
        else:
            if self.claude:
                verdict = self._claude_assess(jumps, npc_kills, mining_signal, current_qty)
                if verdict == "active":
                    self._maybe_activate_belt(
                        score, signals, jumps, npc_kills, current_qty, claude_assisted=True
                    )
                elif verdict == "cleared":
                    self.state.no_change_polls += 1
                    log.info(
                        "Claude assessed 'cleared' in ambiguous range — quiet polls %s/%s",
                        self.state.no_change_polls, self.config.stale_polls_threshold,
                    )
                    self._maybe_clear_belt(score)
                else:
                    log.info("Ambiguous (%s/100) — Claude: ambiguous — holding state", score)
            else:
                log.info("Ambiguous confidence (%s/100) — holding current state", score)

        save_state(self.config.state_file, self.state)
