from __future__ import annotations

import time
from typing import Any

import httpx

from spotify_auth import SpotifyPKCEAuth


class SpotifyClient:
    def __init__(
        self,
        auth: SpotifyPKCEAuth,
        *,
        base_url: str = "https://api.spotify.com/v1",
    ) -> None:
        self.auth = auth
        self.base_url = base_url.rstrip("/")

    def get_current_playback(self) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "/me/player",
            params={"additional_types": "track,episode"},
        )
        if response is None or response.status_code == 204:
            return None
        if response.status_code >= 400:
            self._print_error("current playback", response)
            return None
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except ValueError as exc:
            print(f"Spotify returned invalid playback JSON: {exc}")
            return None

    def get_queue(self) -> dict[str, Any] | None:
        response = self._request("GET", "/me/player/queue")
        if response is None or response.status_code == 204:
            return None
        if response.status_code >= 400:
            self._print_error("queue lookup", response)
            return None
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except ValueError as exc:
            print(f"Spotify returned invalid queue JSON: {exc}")
            return None

    def skip_to_next(self, *, device_id: str | None = None) -> bool:
        params = {"device_id": device_id} if device_id else None
        response = self._request("POST", "/me/player/next", params=params)
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("skip to next", response)
        if response.status_code == 403:
            print("Hint: Spotify requires Premium for playback control endpoints.")
        return False

    def skip_to_previous(self, *, device_id: str | None = None) -> bool:
        params = {"device_id": device_id} if device_id else None
        response = self._request("POST", "/me/player/previous", params=params)
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("skip to previous", response)
        if response.status_code == 403:
            print("Hint: Spotify requires Premium for playback control endpoints.")
        return False

    def start_playback(self, *, device_id: str | None = None) -> bool:
        params = {"device_id": device_id} if device_id else None
        response = self._request("PUT", "/me/player/play", params=params)
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("start playback", response)
        if response.status_code == 403:
            print("Hint: Spotify requires Premium for playback control endpoints.")
        return False

    def pause_playback(self, *, device_id: str | None = None) -> bool:
        params = {"device_id": device_id} if device_id else None
        response = self._request("PUT", "/me/player/pause", params=params)
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("pause playback", response)
        if response.status_code == 403:
            print("Hint: Spotify requires Premium for playback control endpoints.")
        return False

    def save_track(self, track_id: str) -> bool:
        response = self._request(
            "PUT",
            "/me/library",
            params={"uris": f"spotify:track:{track_id}"},
        )
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("save track to library", response)
        if response.status_code in {401, 403}:
            print("Hint: liking tracks requires the user-library-modify scope.")
        return False

    def remove_track(self, track_id: str) -> bool:
        response = self._request(
            "DELETE",
            "/me/library",
            params={"uris": f"spotify:track:{track_id}"},
        )
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("remove track from library", response)
        if response.status_code in {401, 403}:
            print("Hint: unliking tracks requires the user-library-modify scope.")
        return False

    def is_track_saved(self, track_id: str) -> bool:
        response = self._request(
            "GET",
            "/me/library/contains",
            params={"uris": f"spotify:track:{track_id}"},
        )
        if response is None:
            return False
        if response.status_code >= 400:
            self._print_error("check saved track", response)
            if response.status_code in {401, 403}:
                print("Hint: reading liked track state requires the user-library-read scope.")
            return False
        try:
            payload = response.json()
        except ValueError as exc:
            print(f"Spotify returned invalid saved-track JSON: {exc}")
            return False
        return bool(payload[0]) if isinstance(payload, list) and payload else False

    def seek(self, position_ms: int, *, device_id: str | None = None) -> bool:
        params: dict[str, Any] = {"position_ms": max(0, int(position_ms))}
        if device_id:
            params["device_id"] = device_id
        response = self._request("PUT", "/me/player/seek", params=params)
        if response is None:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        self._print_error("seek", response)
        if response.status_code == 403:
            print("Hint: Spotify requires Premium for playback control endpoints.")
        return False

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        headers_from_caller = dict(kwargs.pop("headers", {}) or {})

        for attempt in range(2):
            token = self.auth.get_access_token(force_refresh=attempt > 0)
            headers = dict(headers_from_caller)
            headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/json"

            response = self._send_with_retries(method, url, headers=headers, **kwargs)
            if response is None:
                return None

            if response.status_code != 401:
                return response

            print("Spotify token was rejected; refreshing token and retrying once.")

        return response

    @staticmethod
    def _send_with_retries(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        max_attempts: int = 3,
        **kwargs: Any,
    ) -> httpx.Response | None:
        for attempt in range(1, max_attempts + 1):
            try:
                response = httpx.request(
                    method,
                    url,
                    headers=headers,
                    timeout=httpx.Timeout(20.0, connect=10.0),
                    **kwargs,
                )
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                if attempt < max_attempts:
                    wait_seconds = attempt * 1.5
                    print(
                        "Spotify API network/TLS error; "
                        f"retrying in {wait_seconds:.1f}s ({attempt}/{max_attempts}): {exc}"
                    )
                    time.sleep(wait_seconds)
                    continue
                print(f"Spotify API request failed after retries: {exc}")
                print("Hint: check that api.spotify.com is reachable through your network/proxy/VPN.")
                return None
            except httpx.RequestError as exc:
                print(f"Spotify API request failed: {exc}")
                return None

            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                wait_seconds = _retry_after_seconds(response) or attempt * 1.5
                print(
                    "Spotify API returned a temporary error; "
                    f"retrying in {wait_seconds:.1f}s ({attempt}/{max_attempts})."
                )
                time.sleep(wait_seconds)
                continue

            return response

        return None

    @staticmethod
    def _print_error(action: str, response: httpx.Response) -> None:
        details = response.text.strip()
        print(f"Spotify API failed during {action}: HTTP {response.status_code} {details}")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None
