from __future__ import annotations

import base64
import binascii
import ipaddress
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from cache import ArtistGenderCache
from gender_resolver import ArtistGender, GenderResolver
from spotify_client import SpotifyClient


ShouldSkipFunc = Callable[[list[ArtistGender | dict[str, Any]], dict[str, Any]], tuple[bool, str]]
LikedSongsFunc = Callable[[dict[str, Any], dict[str, Any]], bool]


class LabelRequest(BaseModel):
    spotify_artist_id: str
    gender: str
    name: str | None = None
    group_composition: str | None = None
    artist_role: str | None = None


class LikeTrackRequest(BaseModel):
    track_id: str


class SeekRequest(BaseModel):
    position_ms: int


def create_web_app(
    *,
    spotify: SpotifyClient,
    resolver: GenderResolver,
    cache: ArtistGenderCache,
    config: dict[str, Any],
    should_skip_func: ShouldSkipFunc,
    is_liked_songs_func: LikedSongsFunc,
    verbose: bool = False,
    enable_skip: bool = True,
    web_username: str = "admin",
    web_password: str | None = None,
    require_auth_for_local: bool = False,
) -> FastAPI:
    app = FastAPI(title="Spotify Male Artist Auto Skipper")
    app.state.last_skip_attempt_track_id = None
    app.state.smart_keep_track_id = None

    @app.middleware("http")
    async def require_remote_auth(request: Request, call_next: Callable[..., Any]):
        if (not require_auth_for_local and _request_is_local(request)) or not web_password:
            return await call_next(request)
        if _valid_basic_auth(
            request.headers.get("authorization"),
            username=web_username,
            password=web_password,
        ):
            return await call_next(request)
        return Response(
            "Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Spotify Skipper"'},
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(WEB_HTML)

    @app.get("/api/current")
    def current_playback() -> dict[str, Any]:
        try:
            playback = spotify.get_current_playback()
        except Exception as exc:
            payload = {"status": "error", "message": f"Spotify request failed: {exc}"}
            _log_verbose_status(payload, verbose)
            return payload

        if not playback:
            payload = {"status": "no_playback", "message": "No active Spotify playback."}
            _log_verbose_status(payload, verbose)
            return payload

        if not playback.get("is_playing"):
            payload = {"status": "paused", "message": "Spotify is paused or not playing."}
            _log_verbose_status(payload, verbose)
            return payload

        item = playback.get("item") or {}
        if playback.get("currently_playing_type") != "track" or item.get("type") != "track":
            payload = {
                "status": "not_track",
                "message": "Current Spotify item is not a track.",
                "currently_playing_type": playback.get("currently_playing_type"),
            }
            _log_verbose_status(payload, verbose)
            return payload

        artists = _extract_artists(item)
        artist_results = _resolve_artists(resolver, artists)

        liked_songs = is_liked_songs_func(playback, config)
        if liked_songs:
            would_skip, reason = False, "Liked Songs source is exempt"
        else:
            would_skip, reason = should_skip_func(artist_results, config)
        track_id = str(item.get("id") or "")
        smart_keep_track_id = getattr(app.state, "smart_keep_track_id", None)
        if smart_keep_track_id and smart_keep_track_id != track_id:
            app.state.smart_keep_track_id = None
        elif smart_keep_track_id == track_id and would_skip:
            would_skip, reason = False, "Smart skip stopped on this cached keep track"

        device = playback.get("device") or {}
        action = _apply_skip_decision(
            spotify=spotify,
            playback=playback,
            track_id=track_id,
            would_skip=would_skip,
            reason=reason,
            config=config,
            enable_skip=enable_skip,
            state=app.state,
        )
        queue = _queue_prefetch_payload(
            spotify=spotify,
            resolver=resolver,
            config=config,
            should_skip_func=should_skip_func,
        )
        payload = {
            "status": "ok",
            "track": {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "Unknown track"),
                "album": str((item.get("album") or {}).get("name") or "Unknown album"),
                "image_url": _album_image_url(item),
                "artists": _track_artist_payloads(artists, artist_results),
                "progress_ms": playback.get("progress_ms"),
                "duration_ms": item.get("duration_ms"),
            },
            "device": {
                "name": str(device.get("name") or ""),
                "type": str(device.get("type") or ""),
                "is_restricted": bool(device.get("is_restricted")),
            },
            "context": {
                "type": str((playback.get("context") or {}).get("type") or ""),
                "uri": str((playback.get("context") or {}).get("uri") or ""),
                "liked_songs": liked_songs,
            },
            "artists": [_artist_result_payload(result) for result in artist_results],
            "decision": {
                "would_skip": would_skip,
                "reason": reason,
            },
            "action": action,
            "queue": queue,
        }
        _log_verbose_playback(payload, verbose)
        return payload

    @app.post("/api/label")
    def label_artist(payload: LabelRequest) -> dict[str, Any]:
        try:
            entry = cache.label(
                spotify_artist_id=payload.spotify_artist_id,
                gender=payload.gender,
                name=payload.name,
                group_composition=payload.group_composition,
                artist_role=payload.artist_role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "status": "ok",
            "artist": {
                "spotify_artist_id": payload.spotify_artist_id,
                **entry,
            },
        }

    @app.post("/api/player/next")
    def next_track() -> dict[str, Any]:
        ok = spotify.skip_to_next()
        app.state.last_skip_attempt_track_id = None
        app.state.smart_keep_track_id = None
        return {"status": "ok" if ok else "error", "action": "next", "performed": ok}

    @app.post("/api/player/skip-to-keep")
    def skip_to_keep() -> dict[str, Any]:
        app.state.last_skip_attempt_track_id = None
        app.state.smart_keep_track_id = None
        result = _skip_to_keep(
            spotify=spotify,
            cache=cache,
            config=config,
            should_skip_func=should_skip_func,
            is_liked_songs_func=is_liked_songs_func,
        )
        final_track = result.get("final_track")
        if result.get("stopped_on") == "keep" and isinstance(final_track, dict):
            app.state.smart_keep_track_id = final_track.get("id")
        return result

    @app.post("/api/player/previous")
    def previous_track() -> dict[str, Any]:
        ok = spotify.skip_to_previous()
        app.state.last_skip_attempt_track_id = None
        app.state.smart_keep_track_id = None
        return {"status": "ok" if ok else "error", "action": "previous", "performed": ok}

    @app.post("/api/player/seek")
    def seek_track(payload: SeekRequest) -> dict[str, Any]:
        position_ms = max(0, int(payload.position_ms))
        ok = spotify.seek(position_ms)
        return {
            "status": "ok" if ok else "error",
            "action": "seek",
            "performed": ok,
            "position_ms": position_ms,
        }

    @app.post("/api/tracks/like")
    def like_track(payload: LikeTrackRequest) -> dict[str, Any]:
        track_id = payload.track_id.strip()
        if not track_id:
            raise HTTPException(status_code=400, detail="track_id is required")
        ok = spotify.save_track(track_id)
        return {"status": "ok" if ok else "error", "action": "like", "performed": ok}

    return app


def _extract_artists(item: dict[str, Any]) -> list[dict[str, str]]:
    artists: list[dict[str, str]] = []
    for artist in item.get("artists") or []:
        spotify_id = artist.get("id")
        name = artist.get("name")
        if spotify_id and name:
            artists.append({"id": str(spotify_id), "name": str(name)})
    return artists


def _resolve_artists(
    resolver: GenderResolver,
    artists: list[dict[str, str]],
) -> list[ArtistGender]:
    artist_results: list[ArtistGender] = []
    for artist in artists:
        try:
            artist_results.append(resolver.resolve_artist(artist["id"], artist["name"]))
        except Exception as exc:
            artist_results.append(
                ArtistGender(
                    spotify_artist_id=artist["id"],
                    name=artist["name"],
                    gender="unknown",
                    source=f"error: {exc}",
                    confidence=0.0,
                )
            )
    return artist_results


def _queue_prefetch_payload(
    *,
    spotify: SpotifyClient,
    resolver: GenderResolver,
    config: dict[str, Any],
    should_skip_func: ShouldSkipFunc,
) -> dict[str, Any]:
    limit = _queue_prefetch_limit(config)
    if limit <= 0:
        return {"tracks": [], "prefetch_limit": 0, "error": ""}

    try:
        queue_payload = spotify.get_queue()
    except Exception as exc:
        return {"tracks": [], "prefetch_limit": limit, "error": str(exc)}

    raw_queue = (queue_payload or {}).get("queue", [])
    if not isinstance(raw_queue, list):
        return {"tracks": [], "prefetch_limit": limit, "error": ""}

    tracks: list[dict[str, Any]] = []
    for item in raw_queue:
        if len(tracks) >= limit:
            break
        if not isinstance(item, dict):
            continue
        if item.get("type") != "track" or not item.get("id"):
            continue

        artists = _extract_artists(item)
        artist_results = _resolve_artists(resolver, artists)
        would_skip, reason = should_skip_func(artist_results, config)
        tracks.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "Unknown track"),
                "album": str((item.get("album") or {}).get("name") or "Unknown album"),
                "image_url": _album_image_url(item),
                "artists": _track_artist_payloads(artists, artist_results),
                "artist_results": [_artist_result_payload(result) for result in artist_results],
                "decision": {
                    "would_skip": would_skip,
                    "reason": reason,
                },
            }
        )

    return {"tracks": tracks, "prefetch_limit": limit, "error": ""}


def _queue_prefetch_limit(config: dict[str, Any]) -> int:
    try:
        return max(0, min(10, int(config.get("queue_prefetch_tracks", 3))))
    except (TypeError, ValueError):
        return 3


def _skip_to_keep(
    *,
    spotify: SpotifyClient,
    cache: ArtistGenderCache,
    config: dict[str, Any],
    should_skip_func: ShouldSkipFunc,
    is_liked_songs_func: LikedSongsFunc,
) -> dict[str, Any]:
    limit = _smart_skip_limit(config)
    if bool(config.get("dry_run", False)):
        return {
            "status": "ok",
            "action": "skip_to_keep",
            "performed": False,
            "skipped_count": 0,
            "stopped_on": "dry_run",
            "reason": "dry_run is enabled",
        }

    playback = spotify.get_current_playback()
    if not playback:
        return _skip_to_keep_result(
            status="error",
            performed=False,
            skipped_count=0,
            stopped_on="no_playback",
            reason="No active Spotify playback.",
        )

    blocked_reason = _skip_blocked_reason(playback)
    if blocked_reason:
        return _skip_to_keep_result(
            status="error",
            performed=False,
            skipped_count=0,
            stopped_on="blocked",
            reason=blocked_reason,
        )

    skipped_count = 0
    device_id = str((playback.get("device") or {}).get("id") or "") or None

    previous_track_id = _playback_track_id(playback)
    ok = spotify.skip_to_next(device_id=device_id)
    if not ok:
        return _skip_to_keep_result(
            status="error",
            performed=False,
            skipped_count=0,
            stopped_on="skip_failed",
            reason="Spotify skip_to_next failed.",
        )

    skipped_count += 1
    playback = _wait_for_track_change(
        spotify,
        previous_track_id=previous_track_id,
    )
    if not playback:
        return _skip_to_keep_result(
            status="ok",
            performed=True,
            skipped_count=skipped_count,
            stopped_on="no_playback",
            reason="No active Spotify playback after skip.",
        )

    latest_track_id = _playback_track_id(playback)
    if latest_track_id and latest_track_id == previous_track_id:
        return _skip_to_keep_result(
            status="error",
            performed=True,
            skipped_count=skipped_count,
            stopped_on="track_change_timeout",
            reason="Spotify did not report a new track after skip; stopped to avoid blind skipping.",
        )

    decision = _cached_playback_decision(
        playback=playback,
        cache=cache,
        config=config,
        should_skip_func=should_skip_func,
        is_liked_songs_func=is_liked_songs_func,
    )

    while skipped_count < limit:
        if not decision["would_skip"]:
            return _skip_to_keep_result(
                status="ok",
                performed=True,
                skipped_count=skipped_count,
                stopped_on="keep",
                reason=str(decision["reason"]),
                final_track=decision["track"],
            )

        blocked_reason = _skip_blocked_reason(playback)
        if blocked_reason:
            return _skip_to_keep_result(
                status="error",
                performed=skipped_count > 0,
                skipped_count=skipped_count,
                stopped_on="blocked",
                reason=blocked_reason,
                final_track=decision["track"],
            )

        previous_track_id = _playback_track_id(playback)
        ok = spotify.skip_to_next(device_id=device_id)
        if not ok:
            return _skip_to_keep_result(
                status="error",
                performed=skipped_count > 0,
                skipped_count=skipped_count,
                stopped_on="skip_failed",
                reason="Spotify skip_to_next failed.",
            )

        skipped_count += 1
        playback = _wait_for_track_change(
            spotify,
            previous_track_id=previous_track_id,
        )
        if not playback:
            return _skip_to_keep_result(
                status="ok",
                performed=True,
                skipped_count=skipped_count,
                stopped_on="no_playback",
                reason="No active Spotify playback after skip.",
            )

        latest_track_id = _playback_track_id(playback)
        if latest_track_id and latest_track_id == previous_track_id:
            return _skip_to_keep_result(
                status="error",
                performed=True,
                skipped_count=skipped_count,
                stopped_on="track_change_timeout",
                reason="Spotify did not report a new track after skip; stopped to avoid blind skipping.",
            )

        decision = _cached_playback_decision(
            playback=playback,
            cache=cache,
            config=config,
            should_skip_func=should_skip_func,
            is_liked_songs_func=is_liked_songs_func,
        )

    return _skip_to_keep_result(
        status="ok",
        performed=skipped_count > 0,
        skipped_count=skipped_count,
        stopped_on="limit_reached",
        reason=f"Reached smart skip limit ({limit}).",
    )


def _cached_playback_decision(
    *,
    playback: dict[str, Any],
    cache: ArtistGenderCache,
    config: dict[str, Any],
    should_skip_func: ShouldSkipFunc,
    is_liked_songs_func: LikedSongsFunc,
) -> dict[str, Any]:
    item = playback.get("item") or {}
    if playback.get("currently_playing_type") != "track" or item.get("type") != "track":
        return {
            "would_skip": False,
            "reason": "Current Spotify item is not a track.",
            "track": _playback_track_payload(item, []),
        }

    artists = _extract_artists(item)
    artist_results = _resolve_artists_from_cache(cache, artists)
    if is_liked_songs_func(playback, config):
        would_skip, reason = False, "Liked Songs source is exempt"
    else:
        would_skip, reason = should_skip_func(artist_results, config)

    return {
        "would_skip": would_skip,
        "reason": reason,
        "track": _playback_track_payload(item, artist_results),
    }


def _resolve_artists_from_cache(
    cache: ArtistGenderCache,
    artists: list[dict[str, str]],
) -> list[ArtistGender]:
    results: list[ArtistGender] = []
    for artist in artists:
        entry = cache.get(artist["id"])
        if not entry:
            results.append(
                ArtistGender(
                    spotify_artist_id=artist["id"],
                    name=artist["name"],
                    gender="unknown",
                    source="cache_miss",
                    confidence=0.0,
                )
            )
            continue

        results.append(
            ArtistGender(
                spotify_artist_id=artist["id"],
                name=artist["name"],
                gender=str(entry.get("gender") or "unknown"),
                source=str(entry.get("source") or "cache"),
                confidence=float(entry.get("confidence") or 0.0),
                group_composition=str(entry.get("group_composition") or "not_group"),
                artist_role=str(entry.get("artist_role") or "unknown"),
            )
        )
    return results


def _playback_track_payload(
    item: dict[str, Any],
    artist_results: list[ArtistGender],
) -> dict[str, Any]:
    artists = _extract_artists(item)
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or "Unknown track"),
        "album": str((item.get("album") or {}).get("name") or "Unknown album"),
        "artists": _track_artist_payloads(artists, artist_results),
    }


def _wait_for_track_change(
    spotify: SpotifyClient,
    *,
    previous_track_id: str,
    timeout_seconds: float = 4.0,
    poll_interval_seconds: float = 0.25,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    latest_playback: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        latest_playback = spotify.get_current_playback()
        latest_track_id = _playback_track_id(latest_playback or {})
        if latest_track_id and latest_track_id != previous_track_id:
            return latest_playback
        time.sleep(poll_interval_seconds)
    return latest_playback


def _playback_track_id(playback: dict[str, Any]) -> str:
    item = playback.get("item") or {}
    return str(item.get("id") or "")


def _skip_blocked_reason(playback: dict[str, Any]) -> str:
    device = playback.get("device") or {}
    if device.get("is_restricted"):
        return "Current Spotify device is restricted."

    disallows = (playback.get("actions") or {}).get("disallows") or {}
    if disallows.get("skipping_next"):
        return "Spotify says skipping to the next track is disallowed."

    return ""


def _smart_skip_limit(config: dict[str, Any]) -> int:
    try:
        return max(1, min(25, int(config.get("smart_skip_max_tracks", 10))))
    except (TypeError, ValueError):
        return 10


def _skip_to_keep_result(
    *,
    status: str,
    performed: bool,
    skipped_count: int,
    stopped_on: str,
    reason: str,
    final_track: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "action": "skip_to_keep",
        "performed": performed,
        "skipped_count": skipped_count,
        "stopped_on": stopped_on,
        "reason": reason,
        "final_track": final_track,
    }


def _album_image_url(item: dict[str, Any]) -> str:
    album = item.get("album") or {}
    images = album.get("images") or []
    if not isinstance(images, list):
        return ""
    for image in images:
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])
    return ""


def _artist_result_payload(result: ArtistGender) -> dict[str, Any]:
    return {
        "spotify_artist_id": result.spotify_artist_id,
        "name": result.name,
        "wiki_url": _artist_wiki_url(result.name),
        "gender": result.gender,
        "gender_label": _gender_display_label(result.gender),
        "group_composition": result.group_composition,
        "artist_role": result.artist_role,
        "source": result.source,
        "confidence": result.confidence,
        "needs_gender_label": result.gender == "unknown",
        "needs_group_composition_label": (
            result.gender == "group" and result.group_composition == "unknown"
        ),
    }


def _track_artist_payloads(
    spotify_artists: list[dict[str, str]],
    artist_results: list[ArtistGender],
) -> list[dict[str, str]]:
    results_by_id = {result.spotify_artist_id: result for result in artist_results}
    payloads: list[dict[str, str]] = []
    for artist in spotify_artists:
        spotify_artist_id = artist["id"]
        name = artist["name"]
        result = results_by_id.get(spotify_artist_id)
        gender = result.gender if result else "unknown"
        group_composition = result.group_composition if result else "not_group"
        payloads.append(
            {
                "id": spotify_artist_id,
                "name": name,
                "gender": gender,
                "gender_label": _gender_display_label(gender),
                "group_composition": group_composition,
                "display_label": _track_artist_display_label(gender, group_composition),
                "wiki_url": _artist_wiki_url(name),
            }
        )
    return payloads


def _track_artist_display_label(gender: str, group_composition: str) -> str:
    if gender == "group" and group_composition and group_composition != "not_group":
        return f"group, {group_composition}"
    return _gender_display_label(gender)


def _gender_display_label(gender: str) -> str:
    if gender == "other":
        return "Non-binary"
    return gender


def _artist_wiki_url(name: str) -> str:
    return f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(name)}"


def _request_is_local(request: Request) -> bool:
    client = request.client
    if client is None:
        return False

    host = (client.host or "").strip().lower()
    if host == "localhost":
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_basic_auth(
    authorization: str | None,
    *,
    username: str,
    password: str,
) -> bool:
    if not authorization:
        return False

    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not credentials:
        return False

    try:
        decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    provided_username, separator, provided_password = decoded.partition(":")
    if not separator:
        return False

    return secrets.compare_digest(provided_username, username) and secrets.compare_digest(
        provided_password,
        password,
    )


def _apply_skip_decision(
    *,
    spotify: SpotifyClient,
    playback: dict[str, Any],
    track_id: str,
    would_skip: bool,
    reason: str,
    config: dict[str, Any],
    enable_skip: bool,
    state: Any,
) -> dict[str, Any]:
    if not would_skip:
        return {"name": "keep", "performed": False, "reason": reason}

    if not enable_skip:
        return {"name": "skip_disabled", "performed": False, "reason": reason}

    if bool(config.get("dry_run", False)):
        state.last_skip_attempt_track_id = track_id
        return {"name": "dry_run_skip", "performed": False, "reason": reason}

    if getattr(state, "last_skip_attempt_track_id", None) == track_id:
        return {"name": "already_processed", "performed": False, "reason": reason}

    device = playback.get("device") or {}
    if device.get("is_restricted"):
        state.last_skip_attempt_track_id = track_id
        return {
            "name": "skip_blocked",
            "performed": False,
            "reason": "Current Spotify device is restricted.",
        }

    disallows = (playback.get("actions") or {}).get("disallows") or {}
    if disallows.get("skipping_next"):
        state.last_skip_attempt_track_id = track_id
        return {
            "name": "skip_blocked",
            "performed": False,
            "reason": "Spotify says skipping to the next track is disallowed.",
        }

    state.last_skip_attempt_track_id = track_id
    ok = spotify.skip_to_next(device_id=device.get("id"))
    return {
        "name": "skip" if ok else "skip_failed",
        "performed": bool(ok),
        "reason": reason,
    }


def _log_verbose_status(payload: dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return
    print(payload.get("message") or payload.get("status") or "Unknown playback state")


def _log_verbose_playback(payload: dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return

    track = payload["track"]
    artists = payload["artists"]
    decision = payload["decision"]
    action = payload["action"]
    artist_names = ", ".join(str(artist["name"]) for artist in artists)

    print(f"Now playing: {track['name']} - {artist_names}")
    print(f"Album: {track['album']}")
    for artist in artists:
        group_details = (
            f", group_composition={artist['group_composition']}"
            if artist["gender"] == "group"
            else ""
        )
        role_details = (
            f", artist_role={artist['artist_role']}"
            if artist["artist_role"] != "unknown"
            else ""
        )
        print(
            f"{artist['name']} => {artist['gender_label']}{group_details}{role_details}, "
            f"confidence={float(artist['confidence']):.2f}, source={artist['source']}, "
            f"spotify_artist_id={artist['spotify_artist_id']}"
        )
    decision_label = "would_skip" if decision["would_skip"] else "keep"
    print(f"Decision: {decision_label} ({decision['reason']})")
    print(f"Action: {action['name']} ({action['reason']})")


WEB_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotify Skipper Console</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fb;
      --ink: #17202a;
      --muted: #657080;
      --line: #d8dee8;
      --panel: #ffffff;
      --accent: #167a5b;
      --accent-ink: #ffffff;
      --danger: #b53737;
      --warn: #8a6414;
      --blue: #2d5fa8;
      --shadow: 0 12px 30px rgba(23, 32, 42, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", Arial, sans-serif;
    }

    main {
      width: min(1100px, calc(100vw - 32px));
      margin: 24px auto;
      display: grid;
      gap: 16px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }

    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
    }

    button {
      border: 1px solid var(--line);
      background: #ffffff;
      color: var(--ink);
      min-height: 34px;
      padding: 0 12px;
      border-radius: 6px;
      font: inherit;
      cursor: pointer;
    }

    button:hover {
      border-color: #aeb8c7;
    }

    button.primary {
      background: var(--accent);
      color: var(--accent-ink);
      border-color: var(--accent);
    }

    button.warn {
      border-color: #c99323;
      color: var(--warn);
    }

    button.danger {
      border-color: #d69b9b;
      color: var(--danger);
    }

    .track {
      display: grid;
      grid-template-columns: 136px minmax(0, 1fr);
      gap: 18px;
      align-items: center;
      padding: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .cover {
      width: 136px;
      aspect-ratio: 1;
      border-radius: 6px;
      background: #dfe5ee;
      object-fit: cover;
    }

    .cover-button {
      width: 136px;
      padding: 0;
      border: 0;
      background: transparent;
      border-radius: 6px;
      cursor: zoom-in;
    }

    .cover-button:focus-visible {
      outline: 3px solid rgba(22, 122, 91, 0.35);
      outline-offset: 3px;
    }

    .cover-lightbox {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(10, 14, 20, 0.88);
      z-index: 50;
      cursor: zoom-out;
    }

    .cover-lightbox.open {
      display: flex;
    }

    .cover-lightbox img {
      max-width: min(94vw, 94vh);
      max-height: 94vh;
      border-radius: 8px;
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.45);
      object-fit: contain;
    }

    .track h2 {
      margin: 0 0 8px;
      font-size: 26px;
      line-height: 1.2;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }

    .meta {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }

    .artist-wiki-link {
      color: var(--blue);
      text-decoration: none;
    }

    .artist-wiki-link:hover {
      text-decoration: underline;
    }

    .badges {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      background: #fbfcfe;
      color: var(--muted);
    }

    .badge.skip {
      color: var(--danger);
      border-color: #e3abab;
      background: #fff7f7;
    }

    .badge.keep {
      color: var(--accent);
      border-color: #9fd0bd;
      background: #f3fbf7;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }

    .queue-preview {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 18px;
      display: grid;
      gap: 10px;
    }

    .queue-preview h2 {
      margin: 0;
      font-size: 17px;
      line-height: 1.3;
      letter-spacing: 0;
    }

    .queue-list {
      margin: 0;
      padding-left: 20px;
      display: grid;
      gap: 10px;
    }

    .queue-title {
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .queue-meta {
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .artist {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 12px;
    }

    .artist-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }

    .artist-name {
      margin: 0;
      font-size: 17px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .status {
      font-size: 13px;
      color: var(--muted);
      line-height: 1.5;
      overflow-wrap: anywhere;
    }

    .label-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .label-panel {
      display: grid;
      gap: 8px;
    }

    .label-panel[hidden] {
      display: none;
    }

    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }

    .progress {
      display: grid;
      grid-template-columns: 48px minmax(120px, 1fr) 48px;
      align-items: center;
      gap: 10px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }

    .progress input[type="range"] {
      width: 100%;
      accent-color: var(--accent);
    }

    .empty,
    .error {
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--muted);
    }

    .error {
      border-color: #e3abab;
      color: var(--danger);
    }

    @media (max-width: 640px) {
      main {
        width: min(100vw - 20px, 1100px);
        margin: 14px auto;
      }

      header {
        align-items: flex-start;
        flex-direction: column;
      }

      .track {
        grid-template-columns: 82px minmax(0, 1fr);
        padding: 12px;
      }

      .cover {
        width: 82px;
      }

      .cover-button {
        width: 82px;
      }

      .track h2 {
        font-size: 20px;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Spotify Skipper Console</h1>
      <div class="toolbar">
        <span id="updated">等待刷新</span>
        <button id="refresh" type="button">刷新</button>
      </div>
    </header>
    <section id="content" class="empty">正在读取 Spotify 播放状态。</section>
  </main>
  <div id="cover-lightbox" class="cover-lightbox" aria-hidden="true">
    <img id="cover-lightbox-image" alt="">
  </div>

  <script>
    const content = document.getElementById("content");
    const updated = document.getElementById("updated");
    const refreshButton = document.getElementById("refresh");
    const coverLightbox = document.getElementById("cover-lightbox");
    const coverLightboxImage = document.getElementById("cover-lightbox-image");
    const NORMAL_REFRESH_MS = 3000;
    const FAST_REFRESH_MS = 500;
    const IMMEDIATE_REFRESH_MS = 100;
    const END_REFRESH_WINDOW_MS = 10000;
    let timer = null;
    let currentTrackId = "";
    let pendingTrackChangeFromTrackId = null;
    let isSeeking = false;
    let playerActionInFlight = false;
    const expandedLabelArtistIds = new Set();

    refreshButton.addEventListener("click", () => refresh());
    coverLightbox.addEventListener("click", () => closeCoverLightbox());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeCoverLightbox();
      }
    });

    async function refresh() {
      if (playerActionInFlight) {
        return;
      }
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (isSeeking) {
        timer = setTimeout(refresh, FAST_REFRESH_MS);
        return;
      }
      let data = null;
      try {
        const response = await fetch("/api/current", { cache: "no-store" });
        data = await response.json();
        render(data);
      } catch (error) {
        content.className = "error";
        content.textContent = `读取失败：${error}`;
      } finally {
        updated.textContent = new Date().toLocaleTimeString();
        scheduleNextRefresh(data);
      }
    }

    function scheduleNextRefresh(data) {
      timer = setTimeout(refresh, nextRefreshDelay(data));
    }

    function nextRefreshDelay(data) {
      if (!data || data.status !== "ok") {
        return NORMAL_REFRESH_MS;
      }

      const trackId = String(data.track?.id || "");
      const progressMs = Number(data.track?.progress_ms || 0);
      const durationMs = Number(data.track?.duration_ms || 0);
      const actionName = String(data.action?.name || "");
      const actionPerformed = Boolean(data.action?.performed);
      const timeUntilEndMs = durationMs > 0 && progressMs >= 0
        ? durationMs - progressMs
        : null;
      const nearTrackEnd = durationMs > 0
        && progressMs >= 0
        && timeUntilEndMs <= END_REFRESH_WINDOW_MS;
      const waitingForManualTrackChange = Boolean(pendingTrackChangeFromTrackId)
        && trackId === pendingTrackChangeFromTrackId;

      if (pendingTrackChangeFromTrackId && trackId && trackId !== pendingTrackChangeFromTrackId) {
        pendingTrackChangeFromTrackId = null;
      }

      if (actionPerformed && ["skip", "next", "previous"].includes(actionName)) {
        pendingTrackChangeFromTrackId = trackId || pendingTrackChangeFromTrackId;
        return IMMEDIATE_REFRESH_MS;
      }

      if (nearTrackEnd || waitingForManualTrackChange || actionName === "already_processed") {
        return FAST_REFRESH_MS;
      }

      if (timeUntilEndMs !== null && timeUntilEndMs > END_REFRESH_WINDOW_MS) {
        return Math.min(
          NORMAL_REFRESH_MS,
          Math.max(FAST_REFRESH_MS, timeUntilEndMs - END_REFRESH_WINDOW_MS),
        );
      }

      return NORMAL_REFRESH_MS;
    }

    function render(data) {
      if (data.status !== "ok") {
        content.className = data.status === "error" ? "error" : "empty";
        content.textContent = data.message || data.status;
        return;
      }

      const renderedTrackId = String(data.track.id || "");
      if (currentTrackId && currentTrackId !== renderedTrackId) {
        expandedLabelArtistIds.clear();
      }
      currentTrackId = renderedTrackId;
      const decisionClass = data.decision.would_skip ? "skip" : "keep";
      const decisionText = data.decision.would_skip ? "会跳过" : "保留";
      content.className = "";
      content.innerHTML = `
        <section class="track">
          ${coverHtml(data.track.image_url)}
          <div>
            <h2>${escapeHtml(data.track.name)}</h2>
            <div class="meta">
              ${trackArtistLinks(data.track.artists)}<br>
              ${escapeHtml(data.track.album)}
            </div>
            <div class="badges">
              <span class="badge ${decisionClass}">${decisionText}: ${escapeHtml(data.decision.reason)}</span>
              <span class="badge ${data.action.performed ? "skip" : "keep"}">Action: ${escapeHtml(data.action.name)}</span>
              ${data.context.liked_songs ? '<span class="badge keep">Liked Songs</span>' : ""}
            </div>
            <div class="progress">
              <span id="progress-current">${formatTime(data.track.progress_ms)}</span>
              <input id="track-progress" type="range" min="0"
                max="${Number(data.track.duration_ms || 0)}"
                value="${Number(data.track.progress_ms || 0)}"
                step="1000"
                ${Number(data.track.duration_ms || 0) > 0 ? "" : "disabled"}>
              <span id="progress-duration">${formatTime(data.track.duration_ms)}</span>
            </div>
            <div class="controls">
              <button type="button" data-player-action="previous">Prev</button>
              <button type="button" data-player-action="next">Next</button>
              <button class="warn" type="button" data-player-action="skip-to-keep">跳到保留</button>
              <button class="primary" type="button" data-player-action="like" data-track-id="${escapeAttr(data.track.id)}">Like</button>
            </div>
          </div>
        </section>
        <section class="grid">
          ${data.artists.map((artist) => artistHtml(artist)).join("")}
        </section>
        ${queueHtml(data.queue)}
      `;

      for (const button of content.querySelectorAll("[data-label]")) {
        button.addEventListener("click", () => labelArtist(button));
      }
      for (const button of content.querySelectorAll("[data-player-action]")) {
        button.addEventListener("click", () => playerAction(button));
      }
      for (const button of content.querySelectorAll("[data-edit-labels]")) {
        button.addEventListener("click", () => {
          if (button.dataset.artistId) {
            expandedLabelArtistIds.add(button.dataset.artistId);
          }
          const panel = button.closest(".artist")?.querySelector("[data-label-panel]");
          if (panel) {
            panel.hidden = false;
          }
        });
      }
      const coverButton = content.querySelector("[data-cover-url]");
      if (coverButton) {
        coverButton.addEventListener("click", () => {
          openCoverLightbox(coverButton.dataset.coverUrl || "");
        });
      }
      const progressInput = content.querySelector("#track-progress");
      if (progressInput) {
        progressInput.addEventListener("input", () => {
          isSeeking = true;
          const current = content.querySelector("#progress-current");
          if (current) {
            current.textContent = formatTime(progressInput.value);
          }
        });
        progressInput.addEventListener("change", () => seekTrack(progressInput));
      }
    }

    function coverHtml(url) {
      if (!url) {
        return '<div class="cover"></div>';
      }
      return `
        <button class="cover-button" type="button" data-cover-url="${escapeAttr(url)}" aria-label="Expand album cover">
          <img class="cover" src="${escapeAttr(url)}" alt="">
        </button>
      `;
    }

    function openCoverLightbox(url) {
      if (!url) {
        return;
      }
      coverLightboxImage.src = url;
      coverLightbox.classList.add("open");
      coverLightbox.setAttribute("aria-hidden", "false");
    }

    function closeCoverLightbox() {
      coverLightbox.classList.remove("open");
      coverLightbox.setAttribute("aria-hidden", "true");
      coverLightboxImage.removeAttribute("src");
    }

    function trackArtistLinks(artists) {
      return artists.map((artist) => {
        const label = `${artist.name} (${artist.display_label || artist.gender_label || artist.gender})`;
        if (!artist.wiki_url) {
          return escapeHtml(label);
        }
        return `<a class="artist-wiki-link" href="${escapeAttr(artist.wiki_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
      }).join(", ");
    }

    function queueHtml(queue) {
      const tracks = Array.isArray(queue?.tracks) ? queue.tracks : [];
      const errorHtml = queue?.error
        ? `<div class="status">队列预缓存失败：${escapeHtml(queue.error)}</div>`
        : "";
      if (!tracks.length && !errorHtml) {
        return "";
      }
      return `
        <section class="queue-preview">
          <h2>已预缓存后续 ${tracks.length} 首</h2>
          ${errorHtml}
          <ol class="queue-list">
            ${tracks.map((track) => queueTrackHtml(track)).join("")}
          </ol>
        </section>
      `;
    }

    function queueTrackHtml(track) {
      const decision = track.decision || {};
      const decisionClass = decision.would_skip ? "skip" : "keep";
      const decisionText = decision.would_skip ? "预判跳过" : "预判保留";
      return `
        <li>
          <div class="queue-title">${escapeHtml(track.name || "Unknown track")}</div>
          <div class="queue-meta">
            ${trackArtistLinks(track.artists || [])}<br>
            ${escapeHtml(track.album || "Unknown album")}
          </div>
          <div class="badges">
            <span class="badge ${decisionClass}">${decisionText}: ${escapeHtml(decision.reason || "")}</span>
          </div>
        </li>
      `;
    }

    function artistHtml(artist) {
      const groupDetails = artist.gender === "group"
        ? `，组成：${artist.group_composition}`
        : "";
      const roleDetails = artist.artist_role !== "unknown"
        ? `，角色：${artist.artist_role}`
        : "";
      return `
        <article class="artist">
          <div class="artist-head">
            <h3 class="artist-name">${escapeHtml(artist.name)}</h3>
            <span class="badge">${escapeHtml(artist.gender_label || artist.gender)}</span>
          </div>
          <div class="status">
            confidence=${Number(artist.confidence || 0).toFixed(2)}，
            source=${escapeHtml(artist.source)}${escapeHtml(groupDetails + roleDetails)}<br>
            ${escapeHtml(artist.spotify_artist_id)}
          </div>
          ${labelControls(artist)}
        </article>
      `;
    }

    function labelControls(artist) {
      const shouldShowLabels = artist.needs_gender_label || artist.needs_group_composition_label;
      const labelsExpanded = shouldShowLabels || expandedLabelArtistIds.has(artist.spotify_artist_id);
      const groupControls = artist.gender === "group"
        ? `
          <div class="label-row">
            ${button("group", "全男团体", artist, "all_male")}
            ${button("group", "全女团体", artist, "all_female")}
            ${button("group", "混合团体", artist, "mixed")}
            ${button("group", "团体未知", artist, "unknown")}
          </div>
        `
        : "";
      return `
        <button type="button" data-edit-labels data-artist-id="${escapeAttr(artist.spotify_artist_id)}" ${shouldShowLabels ? "hidden" : ""}>修改</button>
        <div class="label-panel" data-label-panel ${labelsExpanded ? "" : "hidden"}>
          <div class="label-row">
            ${button("male", "男", artist)}
            ${button("female", "女", artist)}
            ${button("other", "其他", artist)}
            ${button("group", "团体", artist)}
            ${button("unknown", "未知", artist)}
            ${button("male", "男配乐", artist, "", "composer_or_score")}
          </div>
          ${groupControls}
        </div>
      `;
    }

    function button(gender, label, artist, groupComposition = "", artistRole = "") {
      const cls = gender === "male" ? "danger" : gender === "group" ? "warn" : "";
      return `
        <button class="${cls}" type="button"
          data-label="1"
          data-artist-id="${escapeAttr(artist.spotify_artist_id)}"
          data-name="${escapeAttr(artist.name)}"
          data-gender="${escapeAttr(gender)}"
          data-group-composition="${escapeAttr(groupComposition)}"
          data-artist-role="${escapeAttr(artistRole)}">${escapeHtml(label)}</button>
      `;
    }

    async function labelArtist(button) {
      const payload = {
        spotify_artist_id: button.dataset.artistId,
        name: button.dataset.name,
        gender: button.dataset.gender,
        group_composition: button.dataset.groupComposition || null,
        artist_role: button.dataset.artistRole || null,
      };
      button.disabled = true;
      try {
        const response = await fetch("/api/label", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || response.statusText);
        }
        expandedLabelArtistIds.delete(payload.spotify_artist_id);
        await refresh();
      } catch (error) {
        alert(`写入失败：${error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function playerAction(button) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      const action = button.dataset.playerAction;
      const endpoint = action === "previous"
        ? "/api/player/previous"
        : action === "next"
          ? "/api/player/next"
          : action === "skip-to-keep"
            ? "/api/player/skip-to-keep"
            : "/api/tracks/like";
      const payload = action === "like"
        ? { track_id: button.dataset.trackId }
        : {};
      const previousTrackId = currentTrackId;

      button.disabled = true;
      playerActionInFlight = true;
      try {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || response.statusText);
        }
        if (action === "previous" || action === "next" || action === "skip-to-keep") {
          pendingTrackChangeFromTrackId = previousTrackId || currentTrackId || null;
        }
        playerActionInFlight = false;
        await refresh();
      } catch (error) {
        alert(`Action failed: ${error}`);
      } finally {
        playerActionInFlight = false;
        button.disabled = false;
      }
    }

    async function seekTrack(input) {
      const positionMs = Math.max(0, Number(input.value || 0));
      input.disabled = true;
      try {
        const response = await fetch("/api/player/seek", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ position_ms: Math.round(positionMs) }),
        });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || response.statusText);
        }
        isSeeking = false;
        await refresh();
      } catch (error) {
        alert(`Seek failed: ${error}`);
      } finally {
        isSeeking = false;
        input.disabled = false;
      }
    }

    function formatTime(valueMs) {
      const totalSeconds = Math.max(0, Math.floor(Number(valueMs || 0) / 1000));
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, "&#096;");
    }

    refresh();
  </script>
</body>
</html>
"""
