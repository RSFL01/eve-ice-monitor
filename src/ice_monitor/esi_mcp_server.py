"""EVE Online ESI MCP server — exposes ~35 ESI tools via FastMCP."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

from .auth import get_valid_token, load_tokens
from .config import load_config
from .prices import MARKET_HUBS, TRACKED_ITEMS, fetch_price_data
from .state import (
    belt_duration_summary,
    format_time,
    load_state,
    record_belt_duration,
    save_state,
)

log = logging.getLogger("ice-monitor")

ESI_BASE = "https://esi.evetech.net/latest"

server = FastMCP("eve-esi")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esi_get(path: str, params: dict | None = None, auth_token: str | None = None, paginated: bool = False) -> list | dict:
    """GET an ESI endpoint, optionally following X-Pages pagination."""
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    url = f"{ESI_BASE}{path}"
    resp = requests.get(url, params=params or {}, headers=headers, timeout=15)
    resp.raise_for_status()
    if not paginated:
        return resp.json()
    total_pages = int(resp.headers.get("X-Pages", 1))
    results = list(resp.json())
    for page in range(2, total_pages + 1):
        p = dict(params or {})
        p["page"] = page
        r = requests.get(url, params=p, headers=headers, timeout=15)
        r.raise_for_status()
        results.extend(r.json())
    return results


def _esi_post(path: str, json_body: list | dict) -> dict | list:
    resp = requests.post(f"{ESI_BASE}{path}", json=json_body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _get_config():
    return load_config()


def _get_token() -> Optional[str]:
    cfg = _get_config()
    if not cfg.esi_client_id or not cfg.esi_client_secret:
        return None
    return get_valid_token(cfg.esi_client_id, cfg.esi_client_secret, cfg.esi_token_file)


def _require_token() -> tuple[Optional[str], Optional[int], str]:
    """Return (token, character_id, error_string). error_string is empty on success."""
    token = _get_token()
    if not token:
        return None, None, "Not logged in to EVE SSO. Run `ice-monitor --login` first."
    cfg = _get_config()
    tokens = load_tokens(cfg.esi_token_file)
    char_id = tokens.character_id if tokens else None
    return token, char_id, ""


# ---------------------------------------------------------------------------
# server_status (1)
# ---------------------------------------------------------------------------

@server.tool()
def get_eve_server_status() -> str:
    """Get current EVE Online Tranquility server status and player count."""
    try:
        data = _esi_get("/status/")
        return (
            f"EVE server: ONLINE\n"
            f"Players online: {data.get('players', 'unknown'):,}\n"
            f"Server version: {data.get('server_version', 'unknown')}\n"
            f"VIP mode: {data.get('vip', False)}"
        )
    except Exception as exc:
        return f"Error fetching server status: {exc}"


# ---------------------------------------------------------------------------
# universe (8)
# ---------------------------------------------------------------------------

@server.tool()
def resolve_names_to_ids(names: list[str]) -> str:
    """Resolve EVE entity names (characters, corps, systems, items, etc.) to their IDs."""
    try:
        data = _esi_post("/universe/ids/", names)
        lines = []
        for category, items in data.items():
            for item in (items or []):
                lines.append(f"{item['name']} ({category}): {item['id']}")
        return "\n".join(lines) if lines else "No matches found."
    except Exception as exc:
        return f"Error resolving names: {exc}"


@server.tool()
def resolve_ids_to_names(ids: list[int]) -> str:
    """Resolve EVE entity IDs to their names."""
    try:
        data = _esi_post("/universe/names/", ids)
        lines = [f"{item['id']} ({item['category']}): {item['name']}" for item in data]
        return "\n".join(lines) if lines else "No results."
    except Exception as exc:
        return f"Error resolving IDs: {exc}"


@server.tool()
def get_system_info(system_id: int) -> str:
    """Get information about a solar system by ID (name, constellation, security status, etc.)."""
    try:
        data = _esi_get(f"/universe/systems/{system_id}/")
        return (
            f"System: {data.get('name')} (ID: {system_id})\n"
            f"Security status: {data.get('security_status', 0):.2f}\n"
            f"Constellation ID: {data.get('constellation_id')}\n"
            f"Star ID: {data.get('star', {}).get('id', 'N/A')}\n"
            f"Planets: {len(data.get('planets', []))}\n"
            f"Stargates: {len(data.get('stargates', []))}"
        )
    except Exception as exc:
        return f"Error fetching system info: {exc}"


@server.tool()
def get_constellation_info(constellation_id: int) -> str:
    """Get information about a constellation by ID."""
    try:
        data = _esi_get(f"/universe/constellations/{constellation_id}/")
        return (
            f"Constellation: {data.get('name')} (ID: {constellation_id})\n"
            f"Region ID: {data.get('region_id')}\n"
            f"Systems: {len(data.get('systems', []))}"
        )
    except Exception as exc:
        return f"Error fetching constellation info: {exc}"


@server.tool()
def get_region_info(region_id: int) -> str:
    """Get information about a region by ID."""
    try:
        data = _esi_get(f"/universe/regions/{region_id}/")
        return (
            f"Region: {data.get('name')} (ID: {region_id})\n"
            f"Constellations: {len(data.get('constellations', []))}\n"
            f"Description: {(data.get('description') or 'N/A')[:200]}"
        )
    except Exception as exc:
        return f"Error fetching region info: {exc}"


@server.tool()
def get_type_info(type_id: int) -> str:
    """Get information about an item type by ID (name, description, volume, etc.)."""
    try:
        data = _esi_get(f"/universe/types/{type_id}/")
        return (
            f"Item: {data.get('name')} (type_id: {type_id})\n"
            f"Group ID: {data.get('group_id')}\n"
            f"Volume: {data.get('volume', 'N/A')} m³\n"
            f"Mass: {data.get('mass', 'N/A')} kg\n"
            f"Published: {data.get('published', False)}\n"
            f"Description: {(data.get('description') or 'N/A')[:300]}"
        )
    except Exception as exc:
        return f"Error fetching type info: {exc}"


@server.tool()
def get_system_jumps() -> str:
    """Get the number of ship jumps in each solar system in the last hour (top 15 busiest)."""
    try:
        data = _esi_get("/universe/system_jumps/")
        top = sorted(data, key=lambda x: x.get("ship_jumps", 0), reverse=True)[:15]
        lines = [f"System {d['system_id']}: {d['ship_jumps']:,} jumps" for d in top]
        return "Top 15 busiest systems by jumps (last hour):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error fetching system jumps: {exc}"


@server.tool()
def get_system_kills() -> str:
    """Get NPC and ship kills per solar system in the last hour (top 15 by total kills)."""
    try:
        data = _esi_get("/universe/system_kills/")
        top = sorted(data, key=lambda x: x.get("npc_kills", 0) + x.get("ship_kills", 0), reverse=True)[:15]
        lines = [
            f"System {d['system_id']}: {d.get('npc_kills', 0):,} NPC kills, {d.get('ship_kills', 0):,} ship kills, {d.get('pod_kills', 0):,} pod kills"
            for d in top
        ]
        return "Top 15 systems by kills (last hour):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error fetching system kills: {exc}"


# ---------------------------------------------------------------------------
# search (1)
# ---------------------------------------------------------------------------

@server.tool()
def search_eve(query: str, categories: str = "character,corporation,alliance,solar_system,inventory_type") -> str:
    """Search EVE Online for characters, corporations, alliances, systems, or items.

    categories: comma-separated list from: character, corporation, alliance, inventory_type, solar_system, station
    """
    try:
        cat_list = [c.strip() for c in categories.split(",")]
        data = _esi_get("/search/", params={"categories": ",".join(cat_list), "search": query, "strict": "false"})
        if not data:
            return f"No results for '{query}'."
        lines = []
        for cat, ids in data.items():
            if ids:
                lines.append(f"{cat}: {ids[:10]} {'(and more...)' if len(ids) > 10 else ''}")
        return "\n".join(lines) if lines else f"No results for '{query}'."
    except Exception as exc:
        return f"Error searching: {exc}"


# ---------------------------------------------------------------------------
# market (5)
# ---------------------------------------------------------------------------

@server.tool()
def get_market_orders(region_id: int, type_id: int, order_type: str = "sell") -> str:
    """Get market orders for an item in a region. order_type: 'sell', 'buy', or 'all'."""
    try:
        data = _esi_get(
            f"/markets/{region_id}/orders/",
            params={"type_id": type_id, "order_type": order_type},
            paginated=True,
        )
        if not data:
            return f"No {order_type} orders found for type_id {type_id} in region {region_id}."
        prices = [o["price"] for o in data]
        lines = [
            f"{order_type.capitalize()} orders for type {type_id} in region {region_id}:",
            f"  Count: {len(data):,}",
            f"  Min price: {min(prices):,.2f} ISK",
            f"  Max price: {max(prices):,.2f} ISK",
            f"  Avg price: {sum(prices)/len(prices):,.2f} ISK",
        ]
        # Show top 5 cheapest sell or highest buy
        if order_type in ("sell", "all"):
            top = sorted(data, key=lambda x: x["price"])[:5]
            lines.append("  Cheapest 5 sell orders:")
            for o in top:
                lines.append(f"    {o['price']:,.2f} ISK × {o['volume_remain']:,} @ station {o['location_id']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching market orders: {exc}"


@server.tool()
def get_market_history(region_id: int, type_id: int) -> str:
    """Get 30-day market price history for an item in a region."""
    try:
        data = _esi_get(f"/markets/{region_id}/history/", params={"type_id": type_id})
        if not data:
            return f"No history for type_id {type_id} in region {region_id}."
        recent = data[-30:]
        lines = [f"Market history for type {type_id} in region {region_id} (last {len(recent)} days):"]
        for d in recent[-7:]:
            lines.append(f"  {d['date']}: avg={d['average']:,.2f}  high={d['highest']:,.2f}  low={d['lowest']:,.2f}  vol={d['volume']:,}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching market history: {exc}"


@server.tool()
def get_market_prices() -> str:
    """Get adjusted and average prices for all tradeable items (top 20 by adjusted price)."""
    try:
        data = _esi_get("/markets/prices/")
        top = sorted(data, key=lambda x: x.get("adjusted_price") or 0, reverse=True)[:20]
        lines = ["Top 20 items by adjusted price:"]
        for item in top:
            lines.append(
                f"  type_id={item['type_id']}: adjusted={item.get('adjusted_price', 0):,.2f}  average={item.get('average_price', 0):,.2f}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching market prices: {exc}"


@server.tool()
def get_item_price_at_hubs(item_name: str, hub: str = "all") -> str:
    """Get sell price data for an item at major trade hubs using the local price cache.

    item_name: one of 'Helium Isotopes', 'Strontium Clathrates', 'Liquid Ozone'
    hub: 'jita', 'amarr', 'dodixie', 'rens', or 'all'
    """
    try:
        hubs = list(MARKET_HUBS.keys()) if hub == "all" else [hub.lower()]
        invalid = [h for h in hubs if h not in MARKET_HUBS]
        if invalid:
            return f"Unknown hub(s): {invalid}. Valid: {list(MARKET_HUBS.keys())}"
        data = fetch_price_data(hubs)
        lines = []
        for h in hubs:
            d = data[h].get(item_name)
            if d and d.get("min") is not None:
                trend = f"{d['trend_pct']:+.1f}%" if d.get("trend_pct") is not None else "N/A"
                lines.append(
                    f"{h.upper()} — {item_name}: min={d['min']:,.0f}  max={d['max']:,.0f}  avg={d['avg']:,.0f} ISK  7d trend={trend}"
                )
            else:
                lines.append(f"{h.upper()} — {item_name}: no data")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching prices: {exc}"


@server.tool()
def get_ice_prices() -> str:
    """Get current sell prices for all tracked ice products (Helium Isotopes, Strontium Clathrates, Liquid Ozone) at all major hubs."""
    try:
        hubs = list(MARKET_HUBS.keys())
        data = fetch_price_data(hubs)
        lines = []
        for hub in hubs:
            lines.append(f"{hub.upper()}:")
            for item in TRACKED_ITEMS:
                d = data[hub].get(item)
                if d and d.get("min") is not None:
                    trend = f"{d['trend_pct']:+.1f}%" if d.get("trend_pct") is not None else "N/A"
                    lines.append(f"  {item}: {d['min']:,.0f} / {d['avg']:,.0f} / {d['max']:,.0f} ISK (min/avg/max)  7d={trend}")
                else:
                    lines.append(f"  {item}: no data")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching ice prices: {exc}"


# ---------------------------------------------------------------------------
# character (7)
# ---------------------------------------------------------------------------

@server.tool()
def get_character_public_info(character_id: int) -> str:
    """Get public information about a character by ID."""
    try:
        data = _esi_get(f"/characters/{character_id}/")
        return (
            f"Character: {data.get('name')} (ID: {character_id})\n"
            f"Corp ID: {data.get('corporation_id')}\n"
            f"Alliance ID: {data.get('alliance_id', 'None')}\n"
            f"Security status: {data.get('security_status', 0):.2f}\n"
            f"Birthday: {data.get('birthday', 'unknown')}\n"
            f"Race ID: {data.get('race_id')}\n"
            f"Bloodline ID: {data.get('bloodline_id')}"
        )
    except Exception as exc:
        return f"Error fetching character info: {exc}"


@server.tool()
def get_character_portrait(character_id: int) -> str:
    """Get portrait URLs for a character by ID."""
    try:
        data = _esi_get(f"/characters/{character_id}/portrait/")
        lines = [f"Portrait URLs for character {character_id}:"]
        for size, url in data.items():
            lines.append(f"  {size}: {url}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching portrait: {exc}"


@server.tool()
def get_character_location() -> str:
    """Get the authenticated character's current location (system, station, structure). Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        data = _esi_get(f"/characters/{char_id}/location/", auth_token=token)
        parts = [f"Character {char_id} location:"]
        parts.append(f"  Solar system ID: {data.get('solar_system_id')}")
        if "station_id" in data:
            parts.append(f"  Station ID: {data['station_id']}")
        if "structure_id" in data:
            parts.append(f"  Structure ID: {data['structure_id']}")
        return "\n".join(parts)
    except Exception as exc:
        return f"Error fetching character location: {exc}"


@server.tool()
def get_character_skills() -> str:
    """Get the authenticated character's trained skills summary. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        data = _esi_get(f"/characters/{char_id}/skills/", auth_token=token)
        skills = data.get("skills", [])
        sp = data.get("total_sp", 0)
        unallocated = data.get("unallocated_sp", 0)
        lines = [
            f"Character {char_id} skills:",
            f"  Total SP: {sp:,}",
            f"  Unallocated SP: {unallocated:,}",
            f"  Skill count: {len(skills)}",
        ]
        # Show level V skills
        level5 = [s for s in skills if s.get("trained_skill_level") == 5]
        lines.append(f"  Level V skills: {len(level5)}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching skills: {exc}"


@server.tool()
def get_character_wallet() -> str:
    """Get the authenticated character's wallet balance in ISK. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        balance = _esi_get(f"/characters/{char_id}/wallet/", auth_token=token)
        return f"Character {char_id} wallet balance: {float(balance):,.2f} ISK"
    except Exception as exc:
        return f"Error fetching wallet: {exc}"


@server.tool()
def get_character_mining_ledger() -> str:
    """Get the authenticated character's mining ledger for the current month. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        data = _esi_get(f"/characters/{char_id}/mining/", auth_token=token, paginated=True)
        if not data:
            return f"No mining entries found for character {char_id}."
        lines = [f"Mining ledger for character {char_id} ({len(data)} entries):"]
        for entry in data[:20]:
            lines.append(
                f"  {entry.get('date')} — type {entry.get('type_id')}: {entry.get('quantity', 0):,} units @ system {entry.get('solar_system_id')}"
            )
        if len(data) > 20:
            lines.append(f"  ... and {len(data) - 20} more entries")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching mining ledger: {exc}"


@server.tool()
def get_character_online_status() -> str:
    """Get the authenticated character's online status and last login time. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        data = _esi_get(f"/characters/{char_id}/online/", auth_token=token)
        return (
            f"Character {char_id} online status:\n"
            f"  Online: {data.get('online', False)}\n"
            f"  Last login: {data.get('last_login', 'unknown')}\n"
            f"  Last logout: {data.get('last_logout', 'unknown')}\n"
            f"  Logins: {data.get('logins', 0)}"
        )
    except Exception as exc:
        return f"Error fetching online status: {exc}"


# ---------------------------------------------------------------------------
# corporation (4)
# ---------------------------------------------------------------------------

@server.tool()
def get_corporation_info(corporation_id: int) -> str:
    """Get public information about a corporation by ID."""
    try:
        data = _esi_get(f"/corporations/{corporation_id}/")
        return (
            f"Corporation: {data.get('name')} [{data.get('ticker')}] (ID: {corporation_id})\n"
            f"Alliance ID: {data.get('alliance_id', 'None')}\n"
            f"CEO character ID: {data.get('ceo_id')}\n"
            f"Member count: {data.get('member_count', 0):,}\n"
            f"Tax rate: {data.get('tax_rate', 0) * 100:.1f}%\n"
            f"Founded: {data.get('date_founded', 'unknown')}\n"
            f"War eligible: {data.get('war_eligible', False)}\n"
            f"Description: {(data.get('description') or 'N/A')[:200]}"
        )
    except Exception as exc:
        return f"Error fetching corporation info: {exc}"


@server.tool()
def get_corporation_members() -> str:
    """Get the member list of the authenticated character's corporation. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        cfg = _get_config()
        tokens = load_tokens(cfg.esi_token_file)
        # Get the character's corp first
        char_data = _esi_get(f"/characters/{char_id}/", auth_token=token)
        corp_id = char_data.get("corporation_id")
        members = _esi_get(f"/corporations/{corp_id}/members/", auth_token=token)
        return f"Corporation {corp_id} has {len(members):,} members.\nMember IDs (first 20): {members[:20]}"
    except Exception as exc:
        return f"Error fetching corporation members: {exc}"


@server.tool()
def get_corporation_structures() -> str:
    """Get structures owned by the authenticated character's corporation. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        char_data = _esi_get(f"/characters/{char_id}/")
        corp_id = char_data.get("corporation_id")
        structures = _esi_get(f"/corporations/{corp_id}/structures/", auth_token=token, paginated=True)
        if not structures:
            return f"No structures found for corporation {corp_id}."
        lines = [f"Corporation {corp_id} structures ({len(structures)} total):"]
        for s in structures[:10]:
            lines.append(
                f"  {s.get('structure_id')} — type {s.get('type_id')} @ system {s.get('system_id')} [{s.get('state', 'unknown')}]"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching structures: {exc}"


@server.tool()
def get_corporation_killmails() -> str:
    """Get recent killmails for the authenticated character's corporation. Requires login."""
    token, char_id, err = _require_token()
    if err:
        return err
    try:
        char_data = _esi_get(f"/characters/{char_id}/")
        corp_id = char_data.get("corporation_id")
        kms = _esi_get(f"/corporations/{corp_id}/killmails/recent/", auth_token=token)
        if not kms:
            return f"No recent killmails for corporation {corp_id}."
        lines = [f"Recent killmails for corporation {corp_id} ({len(kms)} total):"]
        for km in kms[:10]:
            lines.append(f"  killmail_id={km['killmail_id']} hash={km['killmail_hash']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching killmails: {exc}"


# ---------------------------------------------------------------------------
# alliance (2)
# ---------------------------------------------------------------------------

@server.tool()
def get_alliance_info(alliance_id: int) -> str:
    """Get public information about an alliance by ID."""
    try:
        data = _esi_get(f"/alliances/{alliance_id}/")
        return (
            f"Alliance: {data.get('name')} [{data.get('ticker')}] (ID: {alliance_id})\n"
            f"Creator corp ID: {data.get('creator_corporation_id')}\n"
            f"Executor corp ID: {data.get('executor_corporation_id', 'None')}\n"
            f"Founded: {data.get('date_founded', 'unknown')}"
        )
    except Exception as exc:
        return f"Error fetching alliance info: {exc}"


@server.tool()
def get_alliance_corporations(alliance_id: int) -> str:
    """Get the list of corporation IDs in an alliance."""
    try:
        corp_ids = _esi_get(f"/alliances/{alliance_id}/corporations/")
        return f"Alliance {alliance_id} has {len(corp_ids):,} corporations.\nCorp IDs: {corp_ids[:30]}{'...' if len(corp_ids) > 30 else ''}"
    except Exception as exc:
        return f"Error fetching alliance corporations: {exc}"


# ---------------------------------------------------------------------------
# sovereignty (2)
# ---------------------------------------------------------------------------

@server.tool()
def get_sovereignty_map() -> str:
    """Get a summarized sovereignty map — top contested systems and largest holders."""
    try:
        data = _esi_get("/sovereignty/map/")
        claimed = [d for d in data if d.get("alliance_id") or d.get("corporation_id")]
        contested = [d for d in data if d.get("contested", False)]
        # Count by alliance
        alliance_counts: dict[int, int] = {}
        for d in claimed:
            aid = d.get("alliance_id")
            if aid:
                alliance_counts[aid] = alliance_counts.get(aid, 0) + 1
        top_alliances = sorted(alliance_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [
            f"Sovereignty map summary:",
            f"  Total systems claimed: {len(claimed):,}",
            f"  Contested systems: {len(contested):,}",
            f"",
            f"Top 10 alliances by system count:",
        ]
        for aid, count in top_alliances:
            lines.append(f"  Alliance {aid}: {count:,} systems")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching sovereignty map: {exc}"


@server.tool()
def get_sovereignty_structures() -> str:
    """Get sovereignty structures (TCUs, IHubs) — first 30 results."""
    try:
        data = _esi_get("/sovereignty/structures/")
        lines = [f"Sovereignty structures ({len(data):,} total, showing first 30):"]
        for s in data[:30]:
            lines.append(
                f"  system {s.get('solar_system_id')} — type {s.get('structure_type_id')} "
                f"alliance {s.get('alliance_id')} corp {s.get('corporation_id')} "
                f"vuln={s.get('vulnerability_occupancy_level', 'N/A')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching sovereignty structures: {exc}"


# ---------------------------------------------------------------------------
# industry (2)
# ---------------------------------------------------------------------------

@server.tool()
def get_industry_systems() -> str:
    """Get the top 10 solar systems for industry activity by cost index."""
    try:
        data = _esi_get("/industry/systems/")
        # Find systems with highest manufacturing cost index
        def max_index(system):
            return max(
                (a.get("cost_index", 0) for a in system.get("cost_indices", [])),
                default=0,
            )
        top = sorted(data, key=max_index, reverse=True)[:10]
        lines = ["Top 10 industry systems by cost index:"]
        for s in top:
            indices = {a["activity"]: a["cost_index"] for a in s.get("cost_indices", [])}
            mfg = indices.get("manufacturing", 0)
            lines.append(f"  System {s['solar_system_id']}: manufacturing={mfg:.4f}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching industry systems: {exc}"


@server.tool()
def get_industry_facilities() -> str:
    """Get public industry facilities (NPC stations with manufacturing, first 30)."""
    try:
        data = _esi_get("/industry/facilities/")
        lines = [f"Industry facilities ({len(data):,} total, showing first 30):"]
        for f in data[:30]:
            lines.append(
                f"  facility_id={f.get('facility_id')} type={f.get('type_id')} "
                f"system={f.get('solar_system_id')} region={f.get('region_id')} tax={f.get('tax', 0):.2%}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching industry facilities: {exc}"


# ---------------------------------------------------------------------------
# ice_belt (3) — migrated from bot.py
# ---------------------------------------------------------------------------

@server.tool()
def get_belt_status() -> str:
    """Get current ice belt status and respawn timer for Riavayed."""
    try:
        cfg = _get_config()
        state = load_state(cfg.state_file)
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
    except Exception as exc:
        return f"Error fetching belt status: {exc}"


@server.tool()
def mark_belt_cleared() -> str:
    """Mark the Riavayed ice belt as cleared and set the 6h respawn timer."""
    try:
        cfg = _get_config()
        state = load_state(cfg.state_file)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if state.belt_active_since:
            record_belt_duration(state, state.belt_active_since, now)
        state.ice_belt_active = False
        state.belt_cleared_time = now.isoformat()
        state.estimated_respawn_time = (now + timedelta(hours=cfg.respawn_hours)).isoformat()
        state.respawn_alert_sent = False
        save_state(cfg.state_file, state)
        respawn_dt = datetime.fromisoformat(state.estimated_respawn_time)
        return (
            f"Belt marked as cleared in Riavayed.\n"
            f"Cleared at: {format_time(now)}\n"
            f"Est. respawn: {format_time(respawn_dt)}"
        )
    except Exception as exc:
        return f"Error marking belt cleared: {exc}"


@server.tool()
def get_duration_history() -> str:
    """Get historical statistics on how long ice belts have lasted in Riavayed."""
    try:
        cfg = _get_config()
        state = load_state(cfg.state_file)
        return belt_duration_summary(state)
    except Exception as exc:
        return f"Error fetching duration history: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_server(transport: str = "stdio") -> None:
    server.run(transport=transport)


if __name__ == "__main__":
    run_server()
