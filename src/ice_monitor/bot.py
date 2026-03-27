from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import discord

from .discord import send_discord_alert
from .prices import MARKET_HUBS, TRACKED_ITEMS, fetch_price_data
from .state import belt_duration_summary, format_time, load_state, record_belt_duration, save_state

log = logging.getLogger("ice-monitor")

BELT_QUERY_KEYWORDS = {"next belt", "ice belt", "spawn", "when is", "next ice", "belt"}
BELT_DOWN_KEYWORDS = {"belt down", "belt cleared", "belt is down", "belts down", "ice cleared", "ice is down"}

SYSTEM_PROMPT = (
    "You are @ICE#8646, an AI-powered Discord bot built with **Claude Code** by Anthropic, "
    "purpose-built for an EVE Online corporation to monitor the ice belt in Riavayed (system ID 30002993). "
    "You are not a human. When asked, confirm you are an AI powered by Claude, built with Claude Code. "
    "Your Discord name is @ICE#8646.\n\n"
    "What you do: Monitor Riavayed for ice belt activity using ESI signals (jumps, NPC kills) "
    "and an authenticated mining ledger feed. Alert on belt spawns, clears, and respawn windows (~6h cycle). "
    "Track market prices for Helium Isotopes, Strontium Clathrates, and Liquid Ozone at major trade hubs.\n\n"
    "You have tools to fetch live data — use them. Never guess belt status or prices; always call the tool first. "
    "Style: concise, EVE terminology, Discord markdown sparingly, max 4 sentences unless showing data."
)

TOOLS = [
    {
        "name": "get_belt_status",
        "description": "Get current ice belt status and respawn timer for Riavayed",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_price_data",
        "description": (
            "Fetch live market sell prices for Helium Isotopes, Strontium Clathrates, and Liquid Ozone"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hub": {
                    "type": "string",
                    "description": "Trade hub to query, or 'all' for all hubs",
                    "enum": ["jita", "amarr", "dodixie", "rens", "all"],
                }
            },
            "required": ["hub"],
        },
    },
    {
        "name": "mark_belt_cleared",
        "description": (
            "Mark the Riavayed ice belt as cleared and set the 6h respawn timer. "
            "Use only when a user explicitly reports the belt is down/cleared."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_duration_history",
        "description": "Get historical statistics on how long ice belts have lasted in Riavayed",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

_CONV_TTL_SECONDS = 1800    # 30-min idle resets conversation
_MAX_CONV_MESSAGES = 24     # ~12 back-and-forth turns


@dataclass
class _Conversation:
    messages: list = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)


_conversations: dict[int, _Conversation] = {}

_mcp_session = None   # mcp.ClientSession
_mcp_tools = None     # list[dict] in Anthropic format


def _mcp_tool_to_anthropic(tool) -> dict:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.inputSchema}


async def _execute_tool_mcp(tool_name: str, tool_input: dict) -> str:
    result = await _mcp_session.call_tool(tool_name, tool_input)
    return "\n".join(b.text for b in result.content if hasattr(b, "text")) or "(no result)"


def _get_history(user_id: int) -> list:
    """Return the live messages list for user_id, resetting if stale."""
    conv = _conversations.get(user_id)
    if conv is None or (time.monotonic() - conv.last_active) > _CONV_TTL_SECONDS:
        _conversations[user_id] = _Conversation()
    else:
        _conversations[user_id].last_active = time.monotonic()
    return _conversations[user_id].messages


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
        return f"The ice belt is DOWN. Next spawn in approximately {countdown} ({format_time(respawn)})."
    return "Ice belt status is unknown — no spawn timer set yet."


def _format_price_data(data: dict, hubs: list[str]) -> str:
    lines = []
    for hub in hubs:
        lines.append(f"{hub.upper()}:")
        for item in TRACKED_ITEMS:
            d = data[hub].get(item)
            if d and d["min"] is not None:
                trend = f"{d['trend_pct']:+.1f}%" if d["trend_pct"] is not None else "N/A"
                lines.append(
                    f"  {item}: min={d['min']:,.0f}  max={d['max']:,.0f}  avg={d['avg']:,.0f} ISK  7d={trend}"
                )
            else:
                lines.append(f"  {item}: no data")
    return "\n".join(lines)


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


async def _execute_tool(tool_name: str, tool_input: dict, state_file: Path, respawn_hours: int) -> str:
    if tool_name == "get_belt_status":
        return _belt_status_summary(state_file)
    if tool_name == "get_price_data":
        hub = tool_input["hub"]
        hubs = list(MARKET_HUBS.keys()) if hub == "all" else [hub]
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, fetch_price_data, hubs)
        return _format_price_data(data, hubs)
    if tool_name == "mark_belt_cleared":
        return _handle_belt_down(state_file, respawn_hours)
    if tool_name == "get_duration_history":
        return belt_duration_summary(load_state(state_file))
    return f"Unknown tool: {tool_name}"


async def _claude_agentic(
    user_id: int,
    user_message: str,
    client: anthropic.AsyncAnthropic,
    state_file: Path,
    respawn_hours: int,
    max_tokens: int = 600,
) -> str:
    history = _get_history(user_id)
    history.append({"role": "user", "content": user_message})

    active_tools = _mcp_tools if _mcp_tools else TOOLS

    while True:
        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=active_tools,
            messages=history,
        )

        if response.stop_reason != "tool_use":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            history.append({"role": "assistant", "content": response.content})
            if len(history) > _MAX_CONV_MESSAGES:
                history[:] = history[-_MAX_CONV_MESSAGES:]
                while history and history[0]["role"] != "user":
                    history.pop(0)
            return text

        # Execute all tool calls, then loop back for Claude's next step
        history.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if _mcp_session is not None:
                result = await _execute_tool_mcp(block.name, block.input)
            else:
                result = await _execute_tool(block.name, block.input, state_file, respawn_hours)
            log.info("Tool: %s(%s) → %s", block.name, block.input, result[:80])
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        history.append({"role": "user", "content": tool_results})


def run_bot(token: str, state_file: Path, respawn_hours: int = 6, webhook_url: str = "") -> None:
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
        if webhook_url:
            send_discord_alert(webhook_url, "ICE#8646 Online", "I am now awake and watching ice.")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return

        content = message.content.lower()
        user_id = message.author.id
        mentioned = client.user in message.mentions
        log.debug("on_message: user=%s mentioned=%s content=%r", message.author, mentioned, content[:80])

        # @mention → full agentic response with tool use + conversation memory
        if mentioned:
            if not claude:
                await message.reply("⚠️ ANTHROPIC_API_KEY not configured.")
                return
            async with message.channel.typing():
                try:
                    reply = await _claude_agentic(
                        user_id, message.content, claude, state_file, respawn_hours
                    )
                except Exception as exc:
                    log.error("Claude agentic error for user %s: %s", user_id, exc)
                    _conversations.pop(user_id, None)
                    reply = "Something went wrong — conversation reset. Try again."
            await message.reply(reply)
            return

        # Non-@mention: direct command routing
        if any(kw in content for kw in BELT_DOWN_KEYWORDS):
            await message.reply(_handle_belt_down(state_file, respawn_hours))

        elif content.strip().startswith("!timer"):
            await message.reply(_belt_status_summary(state_file))

        elif content.strip().startswith("!avgtime"):
            if claude:
                async with message.channel.typing():
                    reply = await _claude_agentic(
                        user_id, "Summarise the belt duration history.", claude, state_file, respawn_hours
                    )
                await message.reply(reply)
            else:
                await message.reply(belt_duration_summary(load_state(state_file)))

        elif content.strip().startswith("!prices"):
            if claude:
                async with message.channel.typing():
                    reply = await _claude_agentic(
                        user_id, "Show me current prices for all market hubs.", claude, state_file, respawn_hours
                    )
                await message.reply(reply)
            else:
                await message.reply("Set ANTHROPIC_API_KEY or @mention me for prices.")

        elif any(kw in content for kw in BELT_QUERY_KEYWORDS):
            if claude:
                async with message.channel.typing():
                    reply = await _claude_agentic(
                        user_id, message.content, claude, state_file, respawn_hours
                    )
                await message.reply(reply)

    async def _main():
        global _mcp_session, _mcp_tools
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ice_monitor.cli", "--mcp-server"],
            env=dict(os.environ),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    _mcp_session = session
                    _mcp_tools = [_mcp_tool_to_anthropic(t) for t in result.tools]
                    log.info("MCP ready — %d tools", len(_mcp_tools))
                    try:
                        await client.start(token, log_handler=None)
                    finally:
                        await client.close()
        except Exception as e:
            log.warning("MCP startup failed (%s) — using fallback tools", e)
            client.run(token, log_handler=None)

    asyncio.run(_main())
