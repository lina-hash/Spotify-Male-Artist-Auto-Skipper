from __future__ import annotations

import httpx

from spotify_client import SpotifyClient


class FakeAuth:
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "access-token"


def test_save_track_uses_library_endpoint(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return httpx.Response(204)

    monkeypatch.setattr("httpx.request", fake_request)
    client = SpotifyClient(FakeAuth())

    assert client.save_track("track-1") is True

    assert requests[0]["method"] == "PUT"
    assert requests[0]["url"] == "https://api.spotify.com/v1/me/library"
    assert requests[0]["params"] == {"uris": "spotify:track:track-1"}


def test_remove_track_uses_library_endpoint(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return httpx.Response(204)

    monkeypatch.setattr("httpx.request", fake_request)
    client = SpotifyClient(FakeAuth())

    assert client.remove_track("track-1") is True

    assert requests[0]["method"] == "DELETE"
    assert requests[0]["url"] == "https://api.spotify.com/v1/me/library"
    assert requests[0]["params"] == {"uris": "spotify:track:track-1"}


def test_is_track_saved_uses_library_contains_endpoint(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return httpx.Response(200, json=[True])

    monkeypatch.setattr("httpx.request", fake_request)
    client = SpotifyClient(FakeAuth())

    assert client.is_track_saved("track-1") is True

    assert requests[0]["method"] == "GET"
    assert requests[0]["url"] == "https://api.spotify.com/v1/me/library/contains"
    assert requests[0]["params"] == {"uris": "spotify:track:track-1"}
