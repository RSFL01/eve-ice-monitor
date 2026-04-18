"""Tests for market price aggregation in prices.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from ice_monitor import prices
from ice_monitor.prices import (
    MARKET_HUBS,
    TRACKED_ITEMS,
    _get_sell_orders,
    _item_summary,
    fetch_price_data,
    parse_hub_choice,
)


def _mock_response(data: object, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = data
    resp.headers = headers or {}
    return resp


# -------- parse_hub_choice --------

def test_parse_hub_choice_recognizes_named_hub() -> None:
    assert parse_hub_choice("jita please") == ["jita"]


def test_parse_hub_choice_all() -> None:
    assert parse_hub_choice("all hubs") == list(MARKET_HUBS)


def test_parse_hub_choice_multiple() -> None:
    result = parse_hub_choice("compare jita and amarr")
    assert set(result) == {"jita", "amarr"}


def test_parse_hub_choice_unknown_returns_none() -> None:
    assert parse_hub_choice("xyzzy") is None


# -------- _get_sell_orders: pagination via X-Pages --------

def test_get_sell_orders_paginates_and_filters_by_system() -> None:
    page1 = [
        {"price": 100.0, "system_id": 30000142, "volume_remain": 5},
        {"price": 99.0, "system_id": 30002659, "volume_remain": 10},  # different system
    ]
    page2 = [
        {"price": 101.5, "system_id": 30000142, "volume_remain": 3},
    ]

    def fake_get(url: str, params: dict, timeout: int) -> MagicMock:
        page = params.get("page", 1)
        body = page1 if page == 1 else page2
        # X-Pages=2 on page 1, =2 on page 2 (pagination terminates on equality)
        return _mock_response(body, headers={"X-Pages": "2"})

    with patch("ice_monitor.prices.requests.get", side_effect=fake_get):
        orders = _get_sell_orders(region_id=10000002, system_id=30000142, type_id=16262)

    # Only system 30000142 orders retained
    assert len(orders) == 2
    assert all(o["system_id"] == 30000142 for o in orders)
    assert sorted(o["price"] for o in orders) == [100.0, 101.5]


def test_get_sell_orders_single_page() -> None:
    with patch(
        "ice_monitor.prices.requests.get",
        return_value=_mock_response([{"price": 42.0, "system_id": 30000142}], headers={"X-Pages": "1"}),
    ) as mock_get:
        orders = _get_sell_orders(region_id=10000002, system_id=30000142, type_id=16262)

    assert len(orders) == 1
    mock_get.assert_called_once()


# -------- _item_summary: stats and trend --------

def test_item_summary_computes_min_max_avg() -> None:
    orders = [{"price": p, "system_id": 30000142, "volume_remain": 1} for p in (100.0, 150.0, 200.0)]
    # 14 days of history: older week avg = 100, recent week avg = 150 → +50% trend
    history = [{"average": 100.0} for _ in range(7)] + [{"average": 150.0} for _ in range(7)]

    with patch("ice_monitor.prices._get_sell_orders", return_value=orders), \
         patch("ice_monitor.prices._get_history", return_value=history):
        result = _item_summary(region_id=10000002, system_id=30000142, type_id=16262)

    assert result["min"] == 100.0
    assert result["max"] == 200.0
    assert result["avg"] == 150.0
    assert result["trend_pct"] == 50.0


def test_item_summary_no_orders() -> None:
    with patch("ice_monitor.prices._get_sell_orders", return_value=[]), \
         patch("ice_monitor.prices._get_history", return_value=[]):
        result = _item_summary(region_id=10000002, system_id=30000142, type_id=16262)
    assert result["min"] is None
    assert result["max"] is None
    assert result["avg"] is None
    assert result["trend_pct"] is None


def test_item_summary_trend_none_with_insufficient_history() -> None:
    orders = [{"price": 100.0, "system_id": 30000142, "volume_remain": 1}]
    # Only 5 days of history — older window empty
    history = [{"average": 100.0} for _ in range(5)]
    with patch("ice_monitor.prices._get_sell_orders", return_value=orders), \
         patch("ice_monitor.prices._get_history", return_value=history):
        result = _item_summary(region_id=10000002, system_id=30000142, type_id=16262)
    # min/max/avg populated, trend None
    assert result["min"] == 100.0
    assert result["trend_pct"] is None


def test_item_summary_trend_negative() -> None:
    orders = [{"price": 50.0, "system_id": 30000142, "volume_remain": 1}]
    # Older week 200, recent week 100 → -50%
    history = [{"average": 200.0}] * 7 + [{"average": 100.0}] * 7
    with patch("ice_monitor.prices._get_sell_orders", return_value=orders), \
         patch("ice_monitor.prices._get_history", return_value=history):
        result = _item_summary(region_id=10000002, system_id=30000142, type_id=16262)
    assert result["trend_pct"] == -50.0


# -------- fetch_price_data: end-to-end with mocked ESI --------

def test_fetch_price_data_populates_all_hubs_and_items() -> None:
    # Reset cache to ensure the resolve call is made (or at least doesn't blow up)
    prices._type_id_cache.clear()
    prices._type_id_cache.update({
        "Helium Isotopes": 16272,
        "Strontium Clathrates": 16644,
        "Liquid Ozone": 16273,
    })

    summary = {"min": 1000.0, "max": 2000.0, "avg": 1500.0, "trend_pct": 5.0}
    with patch("ice_monitor.prices._item_summary", return_value=summary):
        result = fetch_price_data(["jita", "amarr"])

    assert set(result.keys()) == {"jita", "amarr"}
    for hub in ("jita", "amarr"):
        assert set(result[hub].keys()) == set(TRACKED_ITEMS)
        for item in TRACKED_ITEMS:
            assert result[hub][item] == summary


def test_fetch_price_data_records_failure_as_none() -> None:
    prices._type_id_cache.update({
        "Helium Isotopes": 16272,
        "Strontium Clathrates": 16644,
        "Liquid Ozone": 16273,
    })

    def raise_(*_a: object, **_kw: object) -> None:
        raise RuntimeError("ESI 500")

    with patch("ice_monitor.prices._item_summary", side_effect=raise_):
        result = fetch_price_data(["jita"])

    for item in TRACKED_ITEMS:
        assert result["jita"][item] is None
