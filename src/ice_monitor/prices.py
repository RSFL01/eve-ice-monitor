from __future__ import annotations

import logging

import requests

log = logging.getLogger("ice-monitor")

ESI_BASE = "https://esi.evetech.net/latest"

MARKET_HUBS: dict[str, tuple[int, int]] = {
    "amarr":   (10000043, 30002187),
    "jita":    (10000002, 30000142),
    "rens":    (10000030, 30002510),
    "dodixie": (10000032, 30002659),
}

TRACKED_ITEMS = ["Helium Isotopes", "Strontium Clathrates", "Liquid Ozone"]

_type_id_cache: dict[str, int] = {}


def _resolve_type_ids(names: list[str]) -> dict[str, int]:
    missing = [n for n in names if n not in _type_id_cache]
    if missing:
        resp = requests.post(f"{ESI_BASE}/universe/ids/", json=missing, timeout=15)
        resp.raise_for_status()
        for item in resp.json().get("inventory_types", []):
            _type_id_cache[item["name"]] = item["id"]
    return {n: _type_id_cache[n] for n in names if n in _type_id_cache}


def _get_sell_orders(region_id: int, system_id: int, type_id: int) -> list[dict]:
    orders: list[dict] = []
    page = 1
    while True:
        params: dict[str, str | int] = {
            "type_id": type_id,
            "order_type": "sell",
            "page": page,
        }
        resp = requests.get(
            f"{ESI_BASE}/markets/{region_id}/orders/",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        orders.extend(data)
        if page >= int(resp.headers.get("X-Pages", 1)):
            break
        page += 1
    return [o for o in orders if o.get("system_id") == system_id]


def _get_history(region_id: int, type_id: int) -> list[dict]:
    resp = requests.get(
        f"{ESI_BASE}/markets/{region_id}/history/",
        params={"type_id": type_id},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _item_summary(region_id: int, system_id: int, type_id: int) -> dict:
    orders = _get_sell_orders(region_id, system_id, type_id)
    if orders:
        prices = [o["price"] for o in orders]
        sell_min, sell_max, sell_avg = min(prices), max(prices), sum(prices) / len(prices)
    else:
        sell_min = sell_max = sell_avg = None

    history = _get_history(region_id, type_id)
    recent = history[-7:] if len(history) >= 7 else history
    older = history[-14:-7] if len(history) >= 14 else []
    recent_avg = sum(h["average"] for h in recent) / len(recent) if recent else None
    older_avg = sum(h["average"] for h in older) / len(older) if older else None
    trend_pct = ((recent_avg - older_avg) / older_avg * 100) if (recent_avg and older_avg) else None

    return {"min": sell_min, "max": sell_max, "avg": sell_avg, "trend_pct": trend_pct}


def fetch_price_data(hubs: list[str]) -> dict[str, dict[str, dict | None]]:
    """Return {hub: {item_name: {min, max, avg, trend_pct}}}."""
    type_ids = _resolve_type_ids(TRACKED_ITEMS)
    result: dict[str, dict[str, dict | None]] = {}
    for hub in hubs:
        region_id, system_id = MARKET_HUBS[hub]
        result[hub] = {}
        for name in TRACKED_ITEMS:
            tid = type_ids.get(name)
            if not tid:
                result[hub][name] = None
                continue
            try:
                result[hub][name] = _item_summary(region_id, system_id, tid)
            except Exception as exc:
                log.warning("Price fetch failed %s/%s: %s", hub, name, exc)
                result[hub][name] = None
    return result


def parse_hub_choice(text: str) -> list[str] | None:
    """Parse user input into a list of hub keys. Returns None if unrecognised."""
    text = text.lower().strip()
    if "all" in text:
        return list(MARKET_HUBS)
    chosen = [h for h in MARKET_HUBS if h in text]
    return chosen if chosen else None
