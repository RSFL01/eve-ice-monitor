from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import discord

from .state import belt_duration_summary, format_time, load_state, record_belt_duration, save_state

log = logging.getLogger("ice-monitor")

KEYWORDS = {"next belt", "ice belt", "spawn", "when is", "next ice", "belt"}
BELT_DOWN_KEYWORDS = {"belt down", "belt cleared", "belt is down", "belts down", "ice cleared", "ice is down"}
HELP_KEYWORDS = {
    "what do you do", "what are you", "how do you work", "what can you do",
    "help", "commands", "who are you", "are you ai", "are you an ai",
    "are you a bot", "are you real", "who made you",
}
PRICE_KEYWORDS = {"price", "prices", "how much", "market", "isotopes", "strontium", "liquid ozone"}

# Tracks users waiting for a hub choice: {user_id: channel_id}
_awaiting_hub: dict[int, int] = {}

SYSTEM_PROMPT = (
    "You are @ICE#8646, an AI-powered Discord bot built with **Claude Code** by Anthropic, "
    "purpose-built for an EVE Online corporation to monitor the ice belt in Riavayed (system ID 30002993). "
    "You are not a human. When asked, confirm you are an AI powered by Claude, built with Claude Code. "
    "Your Discord name is @ICE#8646.\n\n"
    "What you do: You monitor Riavayed for ice belt activity using two signals — "
    "public ESI activity stats (jumps and NPC kills per hour) and an authenticated mining ledger feed. "
    "You send Discord alerts when the belt spawns, clears, and 15 minutes before expected respawn (~6h cycle). "
    "You also track market prices for Helium Isotopes, Strontium Clathrates, and Liquid Ozone across major trade hubs. "
    "Commands: !timer (belt status), !avgtime (belt duration history), !prices (market prices), "
    "'belt down/cleared' (manual clear).\n\n"
    "Style: concise, EVE terminology, Discord markdown sparingly, max 4 sentences unless showing data."
)


def _belt_status_summary(state_file: Path) -> str:
    state = load_state(state_file)
    now = datetime.now(timezone.utc)
    if state.ice_belt_active:
        return "The ice belt is currently ACTIVE in Riavayed. Miners should head out now."
    if state.estimated_respawn_time:
        respawn = datetime.fromisoformat(state.estimated_respawn_time.replace("Z", "+00:00"))
        remaining = respawn - now
        total_seconds = int(remaining.total_seconds())
        if total_seconds <= 0:
            overdue_mins = abs(total_seconds) // 60
            return (
                f"The ice belt spawn is OVERDUE — expected {overdue_mins} minute(s) ago. "
                "Belt may already be up; check the scanner."
            )
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        countdown = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        return (
            f"The ice belt is DOWN. Next spawn in approximately {countdown} ({format_time(respawn)})."
        )
    return "Ice belt status is unknown — no spawn timer set yet."


def _full_context(state_file: Path) -> str:
    return (
        f"Belt status: {_belt_status_summary(state_file)}\n"
        f"Duration history: {belt_duration_summary(load_state(state_file))}"
    )


async def _claude(user_message: str, context: str, client: anthropic.AsyncAnthropic, max_tokens: int = 400) -> str:
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        system=f"{SYSTEM_PROMPT}\n\nCurrent context:\n{context}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _handle_belt_down(state_file: Path, respawn_hours: int) -> str:
    state = load_state(state_file)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if state.belt_active_since:
        record_belt_duration(state, state.belt_active_since, now)
    state.ice_belt_active = False
    state.belt_cleared_time = now.isoformat()
    state.estimated_respawn_time = (now + timedelta(hours=respawn_hours)).isoformat()
    state.respawn_alert_sent = False
    save_state(state_file, state)
    respawn_dt = datetime.fromisoformat(state.estimated_respawn_time)
    return (
        f"Belt marked as cleared in Riavayed.\n"
        f"Cleared at: {format_time(now)}\n"
        f"Est. respawn: {format_time(respawn_dt)}"
    )


def run_bot(token: str, state_file: Path, respawn_hours: int = 6) -> None:
    from .prices import fetch_price_data, parse_hub_choice, MARKET_HUBS

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — Claude responses will be unavailable")

    claude = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info("Discord bot online as %s", client.user)

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return

        content = message.content.lower()
        user_id = message.author.id
        mentioned = client.user in message.mentions

        # ── Pending hub choice ────────────────────────────────────────────────
        if user_id in _awaiting_hub:
            hubs = parse_hub_choice(content)
            if hubs:
                del _awaiting_hub[user_id]
                async with message.channel.typing():
                    data = await asyncio.get_event_loop().run_in_executor(None, fetch_price_data, hubs)
                    if claude:
                        from .prices import TRACKED_ITEMS
                        hub_labels = ", ".join(h.capitalize() for h in hubs)
                        raw = []
                        for hub in hubs:
                            raw.append(f"\n{hub.upper()}:")
                            for item in TRACKED_ITEMS:
                                d = data[hub].get(item)
                                if d and d["min"] is not None:
                                    trend = f"{d['trend_pct']:+.1f}%" if d["trend_pct"] is not None else "N/A"
                                    raw.append(f"  {item}: min={d['min']:,.2f} max={d['max']:,.2f} avg={d['avg']:,.2f} ISK  7d={trend}")
                                else:
                                    raw.append(f"  {item}: no data")
                        ctx = _full_context(state_file) + "\n\nRaw price data:\n" + "\n".join(raw)
                        reply = await _claude(f"Format price data for {hub_labels}", ctx, claude, max_tokens=600)
                    else:
                        lines = []
                        for hub in hubs:
                            lines.append(f"**{hub.upper()}**")
                            for item, d in data[hub].items():
                                if d and d["min"] is not None:
                                    trend = f"{d['trend_pct']:+.1f}%" if d["trend_pct"] is not None else "N/A"
                                    lines.append(f"  **{item}**: {d['min']:,.0f} / {d['max']:,.0f} / {d['avg']:,.0f} ISK  7d: {trend}")
                                else:
                                    lines.append(f"  **{item}**: no data")
                        reply = "\n".join(lines)
                await message.reply(reply)
                return
            else:
                del _awaiting_hub[user_id]

        # ── @ICE mentioned — everything goes through Claude ───────────────────
        if mentioned and claude:
            async with message.channel.typing():
                # Execute state-changing commands first, add result to context
                extra = ""
                if any(kw in content for kw in BELT_DOWN_KEYWORDS):
                    result = _handle_belt_down(state_file, respawn_hours)
                    extra = f"\nBelt-down command was just executed. Result:\n{result}"
                elif content.strip().lstrip("<@0123456789>").strip().startswith("!avgtime"):
                    extra = f"\nDuration stats:\n{belt_duration_summary(load_state(state_file))}"
                elif content.strip().lstrip("<@0123456789>").strip().startswith("!prices") or any(kw in content for kw in PRICE_KEYWORDS):
                    _awaiting_hub[user_id] = message.channel.id
                    hub_list = ", ".join(h.capitalize() for h in MARKET_HUBS)
                    prompt = f"Ask the user which market hub they want prices for. Options: {hub_list}, or All."
                    reply = await _claude(prompt, _full_context(state_file), claude)
                    await message.reply(reply)
                    return

                ctx = _full_context(state_file) + extra
                reply = await _claude(message.content, ctx, claude)
            await message.reply(reply)
            return

        # ── Non-@mention command routing ──────────────────────────────────────
        if any(kw in content for kw in BELT_DOWN_KEYWORDS):
            await message.reply(_handle_belt_down(state_file, respawn_hours))

        elif content.strip().startswith("!timer"):
            await message.reply(_belt_status_summary(state_file))

        elif content.strip().startswith("!avgtime"):
            if claude:
                async with message.channel.typing():
                    ctx = _full_context(state_file)
                    reply = await _claude("Interpret and summarise the belt duration history.", ctx, claude)
                await message.reply(reply)
            else:
                await message.reply(belt_duration_summary(load_state(state_file)))

        elif content.strip().startswith("!prices") or any(kw in content for kw in PRICE_KEYWORDS):
            _awaiting_hub[user_id] = message.channel.id
            hub_list = ", ".join(h.capitalize() for h in MARKET_HUBS)
            await message.reply(f"Which market hub would you like prices for?\n**{hub_list}, or All**")

        elif any(kw in content for kw in KEYWORDS) or any(kw in content for kw in HELP_KEYWORDS):
            if claude:
                async with message.channel.typing():
                    reply = await _claude(message.content, _full_context(state_file), claude)
                await message.reply(reply)
            else:
                await message.reply("⚠️ ANTHROPIC_API_KEY not configured.")

    client.run(token, log_handler=None)
