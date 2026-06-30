from __future__ import annotations

import base64
import binascii
import ipaddress
import secrets
from collections.abc import Callable
from typing import Any

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

        liked_songs = is_liked_songs_func(playback, config)
        if liked_songs:
            would_skip, reason = False, "Liked Songs source is exempt"
        else:
            would_skip, reason = should_skip_func(artist_results, config)

        device = playback.get("device") or {}
        action = _apply_skip_decision(
            spotify=spotify,
            playback=playback,
            track_id=str(item.get("id") or ""),
            would_skip=would_skip,
            reason=reason,
            config=config,
            enable_skip=enable_skip,
            state=app.state,
        )
        payload = {
            "status": "ok",
            "track": {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "Unknown track"),
                "album": str((item.get("album") or {}).get("name") or "Unknown album"),
                "image_url": _album_image_url(item),
                "artists": artists,
                "progress_ms": playback.get("progress_ms"),
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
        return {"status": "ok" if ok else "error", "action": "next", "performed": ok}

    @app.post("/api/player/previous")
    def previous_track() -> dict[str, Any]:
        ok = spotify.skip_to_previous()
        app.state.last_skip_attempt_track_id = None
        return {"status": "ok" if ok else "error", "action": "previous", "performed": ok}

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


def _gender_display_label(gender: str) -> str:
    if gender == "other":
        return "Non-binary"
    return gender


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

    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
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

  <script>
    const content = document.getElementById("content");
    const updated = document.getElementById("updated");
    const refreshButton = document.getElementById("refresh");
    let timer = null;

    refreshButton.addEventListener("click", () => refresh());

    async function refresh() {
      try {
        const response = await fetch("/api/current", { cache: "no-store" });
        const data = await response.json();
        render(data);
      } catch (error) {
        content.className = "error";
        content.textContent = `读取失败：${error}`;
      } finally {
        updated.textContent = new Date().toLocaleTimeString();
      }
    }

    function render(data) {
      if (data.status !== "ok") {
        content.className = data.status === "error" ? "error" : "empty";
        content.textContent = data.message || data.status;
        return;
      }

      const decisionClass = data.decision.would_skip ? "skip" : "keep";
      const decisionText = data.decision.would_skip ? "会跳过" : "保留";
      content.className = "";
      content.innerHTML = `
        <section class="track">
          ${coverHtml(data.track.image_url)}
          <div>
            <h2>${escapeHtml(data.track.name)}</h2>
            <div class="meta">
              ${escapeHtml(data.artists.map((artist) => `${artist.name} (${artist.gender_label || artist.gender})`).join(", "))}<br>
              ${escapeHtml(data.track.album)}
            </div>
            <div class="badges">
              <span class="badge ${decisionClass}">${decisionText}: ${escapeHtml(data.decision.reason)}</span>
              <span class="badge ${data.action.performed ? "skip" : "keep"}">Action: ${escapeHtml(data.action.name)}</span>
              ${data.context.liked_songs ? '<span class="badge keep">Liked Songs</span>' : ""}
              ${data.device.name ? `<span class="badge">${escapeHtml(data.device.name)}</span>` : ""}
            </div>
            <div class="controls">
              <button type="button" data-player-action="previous">Prev</button>
              <button type="button" data-player-action="next">Next</button>
              <button class="primary" type="button" data-player-action="like" data-track-id="${escapeAttr(data.track.id)}">Like</button>
            </div>
          </div>
        </section>
        <section class="grid">
          ${data.artists.map((artist) => artistHtml(artist)).join("")}
        </section>
      `;

      for (const button of content.querySelectorAll("[data-label]")) {
        button.addEventListener("click", () => labelArtist(button));
      }
      for (const button of content.querySelectorAll("[data-player-action]")) {
        button.addEventListener("click", () => playerAction(button));
      }
    }

    function coverHtml(url) {
      if (!url) {
        return '<div class="cover"></div>';
      }
      return `<img class="cover" src="${escapeAttr(url)}" alt="">`;
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
      const base = `
        <div class="label-row">
          ${button("male", "男", artist)}
          ${button("female", "女", artist)}
          ${button("other", "其他", artist)}
          ${button("group", "团体", artist)}
          ${button("unknown", "未知", artist)}
          ${button("male", "男配乐", artist, "", "composer_or_score")}
        </div>
      `;

      if (artist.gender !== "group") {
        return base;
      }

      return base + `
        <div class="label-row">
          ${button("group", "全男团体", artist, "all_male")}
          ${button("group", "全女团体", artist, "all_female")}
          ${button("group", "混合团体", artist, "mixed")}
          ${button("group", "团体未知", artist, "unknown")}
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
        await refresh();
      } catch (error) {
        alert(`写入失败：${error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function playerAction(button) {
      const action = button.dataset.playerAction;
      const endpoint = action === "previous"
        ? "/api/player/previous"
        : action === "next"
          ? "/api/player/next"
          : "/api/tracks/like";
      const payload = action === "like"
        ? { track_id: button.dataset.trackId }
        : {};

      button.disabled = true;
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
        await new Promise((resolve) => setTimeout(resolve, 500));
        await refresh();
      } catch (error) {
        alert(`Action failed: ${error}`);
      } finally {
        button.disabled = false;
      }
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
    timer = setInterval(refresh, 3000);
  </script>
</body>
</html>
"""
