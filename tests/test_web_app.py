from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from cache import ArtistGenderCache
from gender_resolver import ArtistGender
from web_app import create_web_app


class FakeSpotify:
    def __init__(self, playback: dict[str, Any] | None) -> None:
        self.playback = playback
        self.skip_calls: list[str | None] = []
        self.previous_calls: list[str | None] = []
        self.saved_track_ids: list[str] = []

    def get_current_playback(self) -> dict[str, Any] | None:
        return self.playback

    def skip_to_next(self, *, device_id: str | None = None) -> bool:
        self.skip_calls.append(device_id)
        return True

    def skip_to_previous(self, *, device_id: str | None = None) -> bool:
        self.previous_calls.append(device_id)
        return True

    def save_track(self, track_id: str) -> bool:
        self.saved_track_ids.append(track_id)
        return True


class FakeResolver:
    def __init__(self, results: dict[str, ArtistGender]) -> None:
        self.results = results

    def resolve_artist(self, spotify_artist_id: str, name: str) -> ArtistGender:
        return self.results[spotify_artist_id]


def test_web_current_returns_track_and_unknown_artist(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Artist",
                    gender="unknown",
                    source="musicbrainz",
                    confidence=0.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "no configured skip condition matched"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["track"]["name"] == "Song A"
    assert payload["artists"][0]["gender"] == "unknown"
    assert payload["artists"][0]["gender_label"] == "unknown"
    assert payload["artists"][0]["needs_gender_label"] is True
    assert payload["action"]["name"] == "keep"


def test_web_label_writes_manual_cache(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(None),
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post(
        "/api/label",
        json={
            "spotify_artist_id": "artist-1",
            "name": "Mystery Artist",
            "gender": "male",
        },
    )

    assert response.status_code == 200
    assert cache.get("artist-1")["gender"] == "male"
    assert cache.get("artist-1")["source"] == "manual"


def test_web_next_endpoint_skips_to_next_track(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(None)
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/player/next")

    assert response.status_code == 200
    assert response.json()["performed"] is True
    assert spotify.skip_calls == [None]


def test_web_previous_endpoint_skips_to_previous_track(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(None)
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/player/previous")

    assert response.status_code == 200
    assert response.json()["performed"] is True
    assert spotify.previous_calls == [None]


def test_web_like_endpoint_saves_track(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(None)
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/tracks/like", json={"track_id": "track-1"})

    assert response.status_code == 200
    assert response.json()["performed"] is True
    assert spotify.saved_track_ids == ["track-1"]


def test_web_current_displays_other_as_non_binary_label(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Non Binary Artist",
                    gender="other",
                    source="musicbrainz",
                    confidence=0.98,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "no configured skip condition matched"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 200
    artist = response.json()["artists"][0]
    assert artist["gender"] == "other"
    assert artist["gender_label"] == "Non-binary"


def test_web_remote_request_requires_auth_when_password_is_set(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
        web_username="admin",
        web_password="secret",
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Spotify Skipper"'


def test_web_remote_request_accepts_basic_auth(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Artist",
                    gender="unknown",
                    source="musicbrainz",
                    confidence=0.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
        web_username="admin",
        web_password="secret",
    )
    client = TestClient(app)

    response = client.get("/api/current", headers=_basic_auth("admin", "secret"))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_web_auth_all_requires_auth_even_for_local_requests(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver({}),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "unused"),
        is_liked_songs_func=lambda playback, config: False,
        web_username="admin",
        web_password="secret",
        require_auth_for_local=True,
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 401


def test_web_verbose_prints_current_playback(tmp_path, capsys) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Artist",
                    gender="unknown",
                    source="musicbrainz",
                    confidence=0.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "no configured skip condition matched"),
        is_liked_songs_func=lambda playback, config: False,
        verbose=True,
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 200
    output = capsys.readouterr().out
    assert "Now playing: Song A - Mystery Artist" in output
    assert "Mystery Artist => unknown" in output
    assert "Decision: keep" in output
    assert "Action: keep" in output


def test_web_current_skips_when_decision_matches(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(_track_playback())
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Artist",
                    gender="male",
                    source="manual",
                    confidence=1.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (True, "male artist detected: Mystery Artist"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.get("/api/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"]["would_skip"] is True
    assert payload["action"]["name"] == "skip"
    assert payload["action"]["performed"] is True
    assert spotify.skip_calls == ["device-1"]


def test_web_current_does_not_repeat_skip_for_same_track(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(_track_playback())
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Artist",
                    gender="male",
                    source="manual",
                    confidence=1.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (True, "male artist detected: Mystery Artist"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    first = client.get("/api/current").json()
    second = client.get("/api/current").json()

    assert first["action"]["name"] == "skip"
    assert second["action"]["name"] == "already_processed"
    assert spotify.skip_calls == ["device-1"]


def _track_playback() -> dict[str, Any]:
    return {
        "is_playing": True,
        "currently_playing_type": "track",
        "progress_ms": 12000,
        "device": {
            "id": "device-1",
            "name": "Desktop",
            "type": "Computer",
            "is_restricted": False,
        },
        "context": {"type": "playlist", "uri": "spotify:playlist:test"},
        "item": {
            "id": "track-1",
            "type": "track",
            "name": "Song A",
            "album": {
                "name": "Album A",
                "images": [{"url": "https://example.test/cover.jpg"}],
            },
            "artists": [{"id": "artist-1", "name": "Mystery Artist"}],
        },
    }


def _basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}
