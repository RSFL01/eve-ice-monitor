from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ESI_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
ESI_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_VERIFY_URL = "https://login.eveonline.com/oauth/verify"
REDIRECT_URI = "http://localhost:65010/callback"
SCOPES = "esi-location.read_location.v1 esi-industry.read_character_mining.v1"

log = logging.getLogger("ice-monitor")


@dataclass
class TokenData:
    access_token: str
    refresh_token: str
    expires_at: float
    character_id: int
    character_name: str


def save_tokens(path: Path, data: TokenData) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": data.access_token,
                "refresh_token": data.refresh_token,
                "expires_at": data.expires_at,
                "character_id": data.character_id,
                "character_name": data.character_name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_tokens(path: Path) -> TokenData | None:
    if not path.exists():
        return None
    return TokenData(**json.loads(path.read_text(encoding="utf-8")))


def get_valid_token(client_id: str, client_secret: str, path: Path) -> str | None:
    """Return a valid access token, refreshing if within 60 s of expiry."""
    data = load_tokens(path)
    if data is None:
        return None

    if time.time() >= data.expires_at - 60:
        try:
            log.info("ESI token expiring — refreshing...")
            resp = requests.post(
                ESI_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": data.refresh_token},
                auth=(client_id, client_secret),
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()
            data.access_token = raw["access_token"]
            data.refresh_token = raw.get("refresh_token", data.refresh_token)
            data.expires_at = time.time() + raw.get("expires_in", 1199)
            save_tokens(path, data)
        except Exception as exc:
            log.warning("Token refresh failed: %s", exc)
            return None

    return data.access_token


def do_login(client_id: str, client_secret: str, token_file: Path) -> TokenData:
    """Run the EVE SSO OAuth2 PKCE login flow."""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    auth_url = (
        f"{ESI_AUTH_URL}?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "scope": SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    captured: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = params.get("code", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful! You can close this window.</h2></body></html>"
            )

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", 65010), _Handler)
    server.timeout = 120

    print("\nOpening browser for EVE SSO login...")
    print(f"If browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)
    server.handle_request()

    if not captured.get("code"):
        raise RuntimeError("No authorization code received")
    if captured.get("state") != state:
        raise RuntimeError("State mismatch — possible CSRF")

    resp = requests.post(
        ESI_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": captured["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()

    verify = requests.get(
        ESI_VERIFY_URL,
        headers={"Authorization": f"Bearer {raw['access_token']}"},
        timeout=15,
    )
    verify.raise_for_status()
    char = verify.json()

    token_data = TokenData(
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        expires_at=time.time() + raw.get("expires_in", 1199),
        character_id=int(char["CharacterID"]),
        character_name=char["CharacterName"],
    )
    save_tokens(token_file, token_data)
    return token_data
