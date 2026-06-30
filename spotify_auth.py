from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from queue import Queue, Empty
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
DEFAULT_SCOPES = [
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-library-modify",
]


class SpotifyPKCEAuth:
    def __init__(
        self,
        client_id: str,
        *,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        scopes: list[str] | None = None,
        token_cache_path: str | Path = "token_cache.json",
    ) -> None:
        self.client_id = client_id.strip()
        self.redirect_uri = redirect_uri
        self.scopes = scopes or DEFAULT_SCOPES
        self.token_cache_path = Path(token_cache_path)

        if not self.client_id:
            raise ValueError("SPOTIFY_CLIENT_ID is required.")

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        token = self._load_token()

        if (
            not force_refresh
            and token.get("access_token")
            and not self._is_expired(token)
        ):
            return str(token["access_token"])

        refresh_token = token.get("refresh_token")
        if refresh_token:
            refreshed = self._refresh_token(str(refresh_token))
            if refreshed:
                return str(refreshed["access_token"])

        authorized = self._authorize_interactively()
        return str(authorized["access_token"])

    def _authorize_interactively(self) -> dict[str, Any]:
        verifier = _new_code_verifier()
        challenge = _code_challenge(verifier)
        state = secrets.token_urlsafe(24)

        auth_params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'scope': ' '.join(self.scopes),
            'code_challenge_method': 'S256',
            'code_challenge': challenge,
            'state': state,
        }
        auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

        receiver = OAuthCallbackReceiver(self.redirect_uri, expected_state=state)
        code = receiver.wait_for_code(auth_url)

        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )

        if response.status_code >= 400:
            raise RuntimeError(f"Spotify token exchange failed: {response.text}")

        token = _with_expiry(response.json())
        self._save_token(token)
        return token

    def _refresh_token(self, refresh_token: str) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
                response = client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            print(f"Spotify token refresh failed: {exc}")
            return None

        if response.status_code >= 400:
            print(f"Spotify token refresh failed: {response.status_code} {response.text}")
            return None

        new_token = _with_expiry(response.json())
        if "refresh_token" not in new_token:
            new_token["refresh_token"] = refresh_token
        self._save_token(new_token)
        return new_token

    def _load_token(self) -> dict[str, Any]:
        if not self.token_cache_path.exists():
            return {}
        try:
            return json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read token cache: {exc}")
            return {}

    def _save_token(self, token: dict[str, Any]) -> None:
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.token_cache_path.with_suffix(self.token_cache_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(token, indent=2), encoding="utf-8")
        tmp_path.replace(self.token_cache_path)

    @staticmethod
    def _is_expired(token: dict[str, Any], skew_seconds: int = 60) -> bool:
        expires_at = float(token.get("expires_at") or 0)
        return time.time() >= expires_at - skew_seconds


class OAuthCallbackReceiver:
    def __init__(self, redirect_uri: str, *, expected_state: str) -> None:
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http":
            raise ValueError("Redirect URI must use http for the local callback.")
        if not parsed.hostname or not parsed.port:
            raise ValueError("Redirect URI must include host and port.")

        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path or "/callback"
        self.expected_state = expected_state

    def wait_for_code(self, auth_url: str, timeout_seconds: int = 300) -> str:
        result_queue: Queue[tuple[str, str]] = Queue(maxsize=1)
        app = FastAPI()

        @app.get(self.path)
        def callback(
            code: str | None = Query(default=None),
            state: str | None = Query(default=None),
            error: str | None = Query(default=None),
        ) -> HTMLResponse:
            if error:
                _put_once(result_queue, ("error", error))
                return HTMLResponse("<h1>Spotify login failed</h1><p>You can close this tab.</p>")
            if state != self.expected_state:
                _put_once(result_queue, ("error", "OAuth state mismatch."))
                return HTMLResponse("<h1>Spotify login failed</h1><p>State mismatch.</p>")
            if not code:
                _put_once(result_queue, ("error", "Missing authorization code."))
                return HTMLResponse("<h1>Spotify login failed</h1><p>Missing code.</p>")

            _put_once(result_queue, ("code", code))
            return HTMLResponse("<h1>Spotify login complete</h1><p>You can close this tab.</p>")

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        print("Opening Spotify login in your browser.")
        print(f"If the browser does not open, paste this URL:\n{auth_url}")
        webbrowser.open(auth_url)

        try:
            kind, value = result_queue.get(timeout=timeout_seconds)
        except Empty as exc:
            raise TimeoutError("Timed out waiting for Spotify OAuth callback.") from exc
        finally:
            server.should_exit = True
            thread.join(timeout=5)

        if kind == "error":
            raise RuntimeError(value)
        return value


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _with_expiry(token: dict[str, Any]) -> dict[str, Any]:
    expires_in = int(token.get("expires_in") or 3600)
    token["expires_at"] = time.time() + expires_in
    return token


def _put_once(queue: Queue[tuple[str, str]], item: tuple[str, str]) -> None:
    if queue.empty():
        queue.put(item)
