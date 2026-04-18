"""Tests for EVE SSO token storage and refresh in auth.py."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from ice_monitor.auth import TokenData, get_valid_token, load_tokens, save_tokens


def _sample_token(expires_in: float = 1200) -> TokenData:
    return TokenData(
        access_token="access-xyz",
        refresh_token="refresh-abc",
        expires_at=time.time() + expires_in,
        character_id=123456,
        character_name="Test Pilot",
    )


# -------- save / load roundtrip --------

def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    original = _sample_token()
    save_tokens(path, original)
    loaded = load_tokens(path)
    assert loaded is not None
    assert loaded.access_token == "access-xyz"
    assert loaded.refresh_token == "refresh-abc"
    assert loaded.character_id == 123456
    assert loaded.character_name == "Test Pilot"
    assert abs(loaded.expires_at - original.expires_at) < 1


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_tokens(tmp_path / "nope.json") is None


# -------- get_valid_token: returns stored token when fresh --------

def test_returns_stored_token_when_not_near_expiry(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=3600))  # 1h away
    token = get_valid_token("cid", "csecret", path)
    assert token == "access-xyz"


# -------- get_valid_token: refreshes within the 60s window --------

def test_refreshes_when_within_60s_of_expiry(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=30))  # < 60s away — triggers refresh

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 1199,
    }

    with patch("ice_monitor.auth.requests.post", return_value=mock_resp) as post:
        token = get_valid_token("cid", "csecret", path)

    assert token == "new-access-token"
    post.assert_called_once()
    # HTTP Basic auth sent with client_id/secret
    _, kwargs = post.call_args
    assert kwargs["auth"] == ("cid", "csecret")
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "refresh-abc"


def test_refresh_persists_rotated_refresh_token(tmp_path: Path) -> None:
    """ESI rotates refresh tokens on each refresh — the new one must be saved to disk."""
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=10))

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "rotated-refresh-token",
        "expires_in": 1199,
    }

    with patch("ice_monitor.auth.requests.post", return_value=mock_resp):
        get_valid_token("cid", "csecret", path)

    reloaded = load_tokens(path)
    assert reloaded is not None
    assert reloaded.refresh_token == "rotated-refresh-token"
    assert reloaded.access_token == "new-access-token"


def test_refresh_keeps_old_refresh_token_if_not_rotated(tmp_path: Path) -> None:
    """If ESI doesn't return a new refresh_token, keep the existing one."""
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=10))

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    # No refresh_token in response
    mock_resp.json.return_value = {
        "access_token": "new-access-token",
        "expires_in": 1199,
    }

    with patch("ice_monitor.auth.requests.post", return_value=mock_resp):
        get_valid_token("cid", "csecret", path)

    reloaded = load_tokens(path)
    assert reloaded is not None
    assert reloaded.refresh_token == "refresh-abc"


# -------- get_valid_token: failure paths --------

def test_returns_none_when_no_token_file(tmp_path: Path) -> None:
    assert get_valid_token("cid", "csecret", tmp_path / "nope.json") is None


def test_returns_none_when_refresh_fails(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=10))

    def raise_(*_a: object, **_kw: object) -> None:
        raise RuntimeError("network down")

    with patch("ice_monitor.auth.requests.post", side_effect=raise_):
        assert get_valid_token("cid", "csecret", path) is None


def test_failed_refresh_does_not_corrupt_stored_tokens(tmp_path: Path) -> None:
    """A failed refresh must leave the on-disk token file untouched."""
    path = tmp_path / "tokens.json"
    save_tokens(path, _sample_token(expires_in=10))
    before = path.read_text(encoding="utf-8")

    with patch("ice_monitor.auth.requests.post", side_effect=RuntimeError("boom")):
        get_valid_token("cid", "csecret", path)

    assert path.read_text(encoding="utf-8") == before
