from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from cache import ArtistGenderCache
from gender_resolver import ArtistGender
from web_app import WEB_HTML, create_web_app


class FakeSpotify:
    def __init__(
        self,
        playback: dict[str, Any] | None,
        queue: dict[str, Any] | None = None,
        playback_sequence: list[dict[str, Any]] | None = None,
    ) -> None:
        self.playback_sequence = playback_sequence or []
        self.playback_index = 0
        self.playback = self.playback_sequence[0] if self.playback_sequence else playback
        self.queue = queue
        self.skip_calls: list[str | None] = []
        self.previous_calls: list[str | None] = []
        self.saved_track_ids: list[str] = []
        self.seek_positions: list[int] = []

    def get_current_playback(self) -> dict[str, Any] | None:
        return self.playback

    def get_queue(self) -> dict[str, Any] | None:
        return self.queue

    def skip_to_next(self, *, device_id: str | None = None) -> bool:
        self.skip_calls.append(device_id)
        if self.playback_sequence and self.playback_index < len(self.playback_sequence) - 1:
            self.playback_index += 1
            self.playback = self.playback_sequence[self.playback_index]
        return True

    def skip_to_previous(self, *, device_id: str | None = None) -> bool:
        self.previous_calls.append(device_id)
        return True

    def save_track(self, track_id: str) -> bool:
        self.saved_track_ids.append(track_id)
        return True

    def seek(self, position_ms: int, *, device_id: str | None = None) -> bool:
        self.seek_positions.append(position_ms)
        return True


class FakeResolver:
    def __init__(self, results: dict[str, ArtistGender]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def resolve_artist(self, spotify_artist_id: str, name: str) -> ArtistGender:
        self.calls.append((spotify_artist_id, name))
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
    assert payload["track"]["duration_ms"] == 180000
    assert payload["track"]["artists"][0]["name"] == "Mystery Artist"
    assert payload["track"]["artists"][0]["gender_label"] == "unknown"
    assert payload["track"]["artists"][0]["display_label"] == "unknown"
    assert payload["artists"][0]["gender"] == "unknown"
    assert payload["artists"][0]["gender_label"] == "unknown"
    assert payload["artists"][0]["wiki_url"].endswith("search=Mystery+Artist")
    assert payload["artists"][0]["needs_gender_label"] is True
    assert payload["action"]["name"] == "keep"


def test_web_track_artist_display_uses_spotify_name_not_cache_name(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="LA",
                    gender="female",
                    source="cache",
                    confidence=1.0,
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "no configured skip condition matched"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    payload = client.get("/api/current").json()

    assert payload["track"]["artists"][0]["name"] == "Mystery Artist"
    assert payload["track"]["artists"][0]["gender_label"] == "female"
    assert payload["track"]["artists"][0]["display_label"] == "female"
    assert payload["track"]["artists"][0]["wiki_url"].endswith("search=Mystery+Artist")
    assert payload["artists"][0]["name"] == "LA"


def test_web_track_artist_display_shows_group_composition(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    app = create_web_app(
        spotify=FakeSpotify(_track_playback()),
        resolver=FakeResolver(
            {
                "artist-1": ArtistGender(
                    spotify_artist_id="artist-1",
                    name="Mystery Group",
                    gender="group",
                    source="manual",
                    confidence=1.0,
                    group_composition="all_female",
                )
            }
        ),
        cache=cache,
        config={},
        should_skip_func=lambda artists, config: (False, "no configured skip condition matched"),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    payload = client.get("/api/current").json()

    assert payload["track"]["artists"][0]["gender"] == "group"
    assert payload["track"]["artists"][0]["group_composition"] == "all_female"
    assert payload["track"]["artists"][0]["display_label"] == "group, all_female"


def test_web_current_prefetches_three_queue_tracks(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    spotify = FakeSpotify(_track_playback(), queue=_queue_payload())
    resolver = FakeResolver(
        {
            "artist-1": ArtistGender(
                spotify_artist_id="artist-1",
                name="Mystery Artist",
                gender="female",
                source="manual",
                confidence=1.0,
            ),
            "queue-artist-1": ArtistGender(
                spotify_artist_id="queue-artist-1",
                name="Queue Artist 1",
                gender="male",
                source="manual",
                confidence=1.0,
            ),
            "queue-artist-2": ArtistGender(
                spotify_artist_id="queue-artist-2",
                name="Queue Artist 2",
                gender="female",
                source="manual",
                confidence=1.0,
            ),
            "queue-artist-3": ArtistGender(
                spotify_artist_id="queue-artist-3",
                name="Queue Artist 3",
                gender="group",
                source="manual",
                confidence=1.0,
                group_composition="all_female",
            ),
            "queue-artist-4": ArtistGender(
                spotify_artist_id="queue-artist-4",
                name="Queue Artist 4",
                gender="male",
                source="manual",
                confidence=1.0,
            ),
        }
    )
    app = create_web_app(
        spotify=spotify,
        resolver=resolver,
        cache=cache,
        config={"queue_prefetch_tracks": 3},
        should_skip_func=lambda artists, config: (
            any(artist.gender == "male" for artist in artists),
            "male artist detected" if any(artist.gender == "male" for artist in artists) else "keep",
        ),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    payload = client.get("/api/current").json()

    assert [track["id"] for track in payload["queue"]["tracks"]] == [
        "queue-track-1",
        "queue-track-2",
        "queue-track-3",
    ]
    assert payload["queue"]["tracks"][0]["artists"][0]["gender"] == "male"
    assert payload["queue"]["tracks"][0]["decision"]["would_skip"] is True
    assert payload["queue"]["tracks"][2]["artists"][0]["display_label"] == "group, all_female"
    assert ("queue-artist-4", "Queue Artist 4") not in resolver.calls


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


def test_web_skip_to_keep_endpoint_uses_cache_until_keep_track(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    cache.label("current-artist", "male", name="Current Artist")
    cache.label("male-artist-1", "male", name="Male Artist 1")
    cache.label("male-artist-2", "male", name="Male Artist 2")
    cache.label("female-artist", "female", name="Female Artist")
    spotify = FakeSpotify(
        None,
        playback_sequence=[
            _track_playback_for("current-track", "Current Song", "current-artist", "Current Artist"),
            _track_playback_for("skip-track-1", "Skip Song 1", "male-artist-1", "Male Artist 1"),
            _track_playback_for("skip-track-2", "Skip Song 2", "male-artist-2", "Male Artist 2"),
            _track_playback_for("keep-track", "Keep Song", "female-artist", "Female Artist"),
        ],
    )
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={"smart_skip_max_tracks": 10},
        should_skip_func=lambda artists, config: (
            any(artist.gender == "male" for artist in artists),
            "male artist detected" if any(artist.gender == "male" for artist in artists) else "keep",
        ),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/player/skip-to-keep")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["performed"] is True
    assert payload["skipped_count"] == 3
    assert payload["stopped_on"] == "keep"
    assert payload["final_track"]["id"] == "keep-track"
    assert payload["final_track"]["artists"][0]["gender"] == "female"
    assert spotify.skip_calls == ["device-1", "device-1", "device-1"]


def test_web_skip_to_keep_skips_current_track_before_judging(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    cache.label("current-artist", "female", name="Current Artist")
    spotify = FakeSpotify(
        None,
        playback_sequence=[
            _track_playback_for("current-track", "Current Song", "current-artist", "Current Artist"),
            _track_playback_for("next-track", "Next Song", "male-artist", "Male Artist"),
        ],
    )
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={"smart_skip_max_tracks": 10},
        should_skip_func=lambda artists, config: (
            any(artist.gender == "male" for artist in artists),
            "male artist detected" if any(artist.gender == "male" for artist in artists) else "keep",
        ),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/player/skip-to-keep")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["performed"] is True
    assert payload["skipped_count"] == 1
    assert payload["stopped_on"] == "keep"
    assert payload["final_track"]["id"] == "next-track"
    assert spotify.skip_calls == ["device-1"]


def test_web_current_keeps_track_selected_by_skip_to_keep(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    cache.label("current-artist", "male", name="Current Artist")
    cache.label("female-artist", "female", name="Female Artist")
    spotify = FakeSpotify(
        None,
        playback_sequence=[
            _track_playback_for("current-track", "Current Song", "current-artist", "Current Artist"),
            _track_playback_for("keep-track", "Keep Song", "female-artist", "Female Artist"),
        ],
    )
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver(
            {
                "female-artist": ArtistGender(
                    spotify_artist_id="female-artist",
                    name="Female Artist",
                    gender="male",
                    source="test",
                    confidence=1.0,
                )
            }
        ),
        cache=cache,
        config={"smart_skip_max_tracks": 10},
        should_skip_func=lambda artists, config: (
            any(artist.gender == "male" for artist in artists),
            "male artist detected" if any(artist.gender == "male" for artist in artists) else "keep",
        ),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    skip_response = client.post("/api/player/skip-to-keep")
    current_response = client.get("/api/current")

    assert skip_response.status_code == 200
    assert skip_response.json()["final_track"]["id"] == "keep-track"
    payload = current_response.json()
    assert payload["track"]["id"] == "keep-track"
    assert payload["decision"]["would_skip"] is False
    assert payload["decision"]["reason"] == "Smart skip stopped on this cached keep track"
    assert payload["action"]["name"] == "keep"
    assert spotify.skip_calls == ["device-1"]


def test_web_skip_to_keep_stops_when_track_change_is_not_confirmed(tmp_path, monkeypatch) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    cache.label("current-artist", "male", name="Current Artist")
    spotify = FakeSpotify(
        _track_playback_for("current-track", "Current Song", "current-artist", "Current Artist")
    )

    def fake_wait_for_track_change(spotify, *, previous_track_id):
        return spotify.get_current_playback()

    monkeypatch.setattr("web_app._wait_for_track_change", fake_wait_for_track_change)
    app = create_web_app(
        spotify=spotify,
        resolver=FakeResolver({}),
        cache=cache,
        config={"smart_skip_max_tracks": 10},
        should_skip_func=lambda artists, config: (
            any(artist.gender == "male" for artist in artists),
            "male artist detected" if any(artist.gender == "male" for artist in artists) else "keep",
        ),
        is_liked_songs_func=lambda playback, config: False,
    )
    client = TestClient(app)

    response = client.post("/api/player/skip-to-keep")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["performed"] is True
    assert payload["skipped_count"] == 1
    assert payload["stopped_on"] == "track_change_timeout"
    assert spotify.skip_calls == ["device-1"]


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


def test_web_seek_endpoint_changes_track_position(tmp_path) -> None:
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

    response = client.post("/api/player/seek", json={"position_ms": 65000})

    assert response.status_code == 200
    assert response.json()["performed"] is True
    assert response.json()["position_ms"] == 65000
    assert spotify.seek_positions == [65000]


def test_web_html_uses_adaptive_refresh_intervals() -> None:
    assert "const NORMAL_REFRESH_MS = 3000;" in WEB_HTML
    assert "const FAST_REFRESH_MS = 500;" in WEB_HTML
    assert "const IMMEDIATE_REFRESH_MS = 100;" in WEB_HTML
    assert "END_REFRESH_WINDOW_MS" in WEB_HTML
    assert "pendingTrackChangeFromTrackId = trackId || pendingTrackChangeFromTrackId" in WEB_HTML
    assert "setTimeout(refresh, nextRefreshDelay(data))" in WEB_HTML
    assert "setInterval(refresh, 3000)" not in WEB_HTML
    assert "playerActionInFlight" in WEB_HTML


def test_web_html_has_seekable_progress_bar() -> None:
    assert 'id="track-progress"' in WEB_HTML
    assert 'id="progress-current"' in WEB_HTML
    assert 'id="progress-duration"' in WEB_HTML
    assert '"/api/player/seek"' in WEB_HTML
    assert "function formatTime" in WEB_HTML
    assert "isSeeking" in WEB_HTML


def test_web_html_has_skip_to_keep_button() -> None:
    assert 'data-player-action="skip-to-keep"' in WEB_HTML
    assert '"/api/player/skip-to-keep"' in WEB_HTML
    assert "跳到保留" in WEB_HTML


def test_web_html_has_expandable_album_cover() -> None:
    assert 'id="cover-lightbox"' in WEB_HTML
    assert 'data-cover-url' in WEB_HTML
    assert "openCoverLightbox" in WEB_HTML
    assert "closeCoverLightbox" in WEB_HTML


def test_web_html_links_track_artists_to_wikipedia() -> None:
    assert "function trackArtistLinks" in WEB_HTML
    assert "artist-wiki-link" in WEB_HTML
    assert "artist.display_label" in WEB_HTML
    assert 'target="_blank"' in WEB_HTML
    assert 'rel="noopener noreferrer"' in WEB_HTML


def test_web_html_displays_prefetched_queue_tracks() -> None:
    assert "function queueHtml" in WEB_HTML
    assert "function queueTrackHtml" in WEB_HTML
    assert "已预缓存后续" in WEB_HTML
    assert "预判跳过" in WEB_HTML
    assert "预判保留" in WEB_HTML
    artist_grid_index = WEB_HTML.index(
        '${data.artists.map((artist) => artistHtml(artist)).join("")}'
    )
    queue_index = WEB_HTML.index("${queueHtml(data.queue)}")
    assert artist_grid_index < queue_index


def test_web_html_does_not_show_device_name_badge() -> None:
    assert "Device:" not in WEB_HTML


def test_web_html_hides_label_controls_until_edit_for_confirmed_artists() -> None:
    assert "data-edit-labels" in WEB_HTML
    assert "data-label-panel" in WEB_HTML
    assert "artist.needs_gender_label || artist.needs_group_composition_label" in WEB_HTML
    assert "const expandedLabelArtistIds = new Set();" in WEB_HTML
    assert "expandedLabelArtistIds.add(button.dataset.artistId)" in WEB_HTML
    assert "expandedLabelArtistIds.has(artist.spotify_artist_id)" in WEB_HTML
    assert "expandedLabelArtistIds.clear()" in WEB_HTML
    assert "expandedLabelArtistIds.delete(payload.spotify_artist_id)" in WEB_HTML
    assert "panel.hidden = false" in WEB_HTML
    assert "button.hidden = true" not in WEB_HTML


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
            "duration_ms": 180000,
            "album": {
                "name": "Album A",
                "images": [{"url": "https://example.test/cover.jpg"}],
            },
            "artists": [{"id": "artist-1", "name": "Mystery Artist"}],
        },
    }


def _track_playback_for(
    track_id: str,
    track_name: str,
    artist_id: str,
    artist_name: str,
) -> dict[str, Any]:
    playback = _track_playback()
    playback["item"] = {
        "id": track_id,
        "type": "track",
        "name": track_name,
        "duration_ms": 180000,
        "album": {"name": f"{track_name} Album", "images": []},
        "artists": [{"id": artist_id, "name": artist_name}],
    }
    return playback


def _queue_payload() -> dict[str, Any]:
    return {
        "currently_playing": None,
        "queue": [
            {"id": "episode-1", "type": "episode", "name": "Podcast Episode"},
            _queue_track("queue-track-1", "Queue Song 1", "queue-artist-1", "Queue Artist 1"),
            _queue_track("queue-track-2", "Queue Song 2", "queue-artist-2", "Queue Artist 2"),
            _queue_track("queue-track-3", "Queue Song 3", "queue-artist-3", "Queue Artist 3"),
            _queue_track("queue-track-4", "Queue Song 4", "queue-artist-4", "Queue Artist 4"),
        ],
    }


def _queue_track(
    track_id: str,
    track_name: str,
    artist_id: str,
    artist_name: str,
) -> dict[str, Any]:
    return {
        "id": track_id,
        "type": "track",
        "name": track_name,
        "duration_ms": 180000,
        "album": {"name": f"{track_name} Album", "images": []},
        "artists": [{"id": artist_id, "name": artist_name}],
    }


def _basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}
