from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False

from cache import (
    ArtistGenderCache,
    VALID_ARTIST_ROLES,
    VALID_GENDERS,
    VALID_GROUP_COMPOSITIONS,
    normalize_artist_role,
    normalize_gender,
    normalize_group_composition,
)
from gender_resolver import (
    DEFAULT_MUSICBRAINZ_USER_AGENT,
    ArtistGender,
    GenderResolver,
)


DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
DEFAULT_SCOPES = [
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-library-modify",
]


DEFAULT_CONFIG = {
    "poll_interval_seconds": 3,
    "queue_prefetch_tracks": 3,
    "smart_skip_max_tracks": 10,
    "skip_if_any_artist_male": True,
    "skip_unknown": False,
    "skip_groups": False,
    "skip_all_male_groups": False,
    "keep_male_composers": True,
    "keep_liked_songs": True,
    "prompt_on_unknown": False,
    "dry_run": False,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "label":
        return label_artist(args)
    if args.command == "web":
        return run_web_client(args)

    return run_watcher(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automatically skip Spotify tracks by male solo artists."
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start polling Spotify playback.")
    run_parser.add_argument("--config", default="config.json", help="Path to config.json.")
    run_parser.add_argument(
        "--cache", default="artist_gender_cache.json", help="Path to artist gender cache."
    )
    run_parser.add_argument(
        "--token-cache", default="token_cache.json", help="Path to Spotify token cache."
    )
    run_parser.add_argument("--once", action="store_true", help="Run one polling cycle.")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decisions without calling Spotify skip.",
    )
    run_parser.add_argument(
        "--prompt-on-unknown",
        action="store_true",
        help="Prompt to manually label unknown artists while running.",
    )

    web_parser = subparsers.add_parser("web", help="Start the local web console.")
    web_parser.add_argument("--config", default="config.json", help="Path to config.json.")
    web_parser.add_argument(
        "--cache", default="artist_gender_cache.json", help="Path to artist gender cache."
    )
    web_parser.add_argument(
        "--token-cache", default="token_cache.json", help="Path to Spotify token cache."
    )
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the local web console.",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8890,
        help="Port for the local web console. Avoid 8888 because Spotify OAuth uses it.",
    )
    web_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the web console in the default browser.",
    )
    web_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show skip decisions in the web console without calling Spotify skip.",
    )
    web_parser.add_argument(
        "--web-username",
        default=None,
        help="Username for non-local web access. Defaults to WEB_AUTH_USERNAME or admin.",
    )
    web_parser.add_argument(
        "--web-password",
        default=None,
        help="Password for non-local web access. Defaults to WEB_AUTH_PASSWORD or a temporary password.",
    )
    web_parser.add_argument(
        "--auth-all",
        action="store_true",
        help="Require web login for every request, including localhost. Use this behind public tunnels.",
    )
    web_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print current playback and decisions whenever the web page refreshes.",
    )

    label_parser = subparsers.add_parser("label", help="Manually label an artist.")
    label_parser.add_argument("spotify_artist_id")
    label_parser.add_argument("gender", choices=sorted(VALID_GENDERS))
    label_parser.add_argument("--name", help="Optional artist display name.")
    label_parser.add_argument(
        "--group-composition",
        choices=sorted(VALID_GROUP_COMPOSITIONS - {"not_group"}),
        help="Optional group composition when gender is group.",
    )
    label_parser.add_argument(
        "--artist-role",
        choices=sorted(VALID_ARTIST_ROLES),
        help="Optional artist role, e.g. composer_or_score.",
    )
    label_parser.add_argument(
        "--cache", default="artist_gender_cache.json", help="Path to artist gender cache."
    )

    parser.set_defaults(command="run")
    return parser


def run_watcher(args: argparse.Namespace) -> int:
    load_dotenv()
    config = load_config(Path(getattr(args, "config", "config.json")))
    if getattr(args, "dry_run", False):
        config["dry_run"] = True
    if getattr(args, "prompt_on_unknown", False):
        config["prompt_on_unknown"] = True

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
    user_agent = os.getenv("MUSICBRAINZ_USER_AGENT", DEFAULT_MUSICBRAINZ_USER_AGENT).strip()

    if not client_id or client_id == "your_spotify_client_id_here":
        print_first_run_setup()
        return 2

    from spotify_auth import SpotifyPKCEAuth
    from spotify_client import SpotifyClient

    auth = SpotifyPKCEAuth(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=DEFAULT_SCOPES,
        token_cache_path=getattr(args, "token_cache", "token_cache.json"),
    )
    spotify = SpotifyClient(auth)
    cache = ArtistGenderCache(getattr(args, "cache", "artist_gender_cache.json"))
    resolver = GenderResolver(cache, user_agent=user_agent)
    state: dict[str, Any] = {"last_track_id": None}

    print("Spotify Male Artist Auto Skipper is running. Press Ctrl+C to stop.")
    print(f"Poll interval: {config['poll_interval_seconds']}s")
    if config["dry_run"]:
        print("Dry run is ON: skip decisions will be printed but not sent to Spotify.")
    if config["prompt_on_unknown"]:
        print("Runtime unknown prompt is ON: unknown artists can be labeled immediately.")

    try:
        while True:
            process_playback_once(spotify, resolver, config, state)
            if getattr(args, "once", False):
                break
            time.sleep(float(config["poll_interval_seconds"]))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        resolver.close()

    return 0


def run_web_client(args: argparse.Namespace) -> int:
    load_dotenv()
    config = load_config(Path(getattr(args, "config", "config.json")))
    if getattr(args, "dry_run", False):
        config["dry_run"] = True

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
    user_agent = os.getenv("MUSICBRAINZ_USER_AGENT", DEFAULT_MUSICBRAINZ_USER_AGENT).strip()

    if not client_id or client_id == "your_spotify_client_id_here":
        print_first_run_setup()
        return 2

    from spotify_auth import SpotifyPKCEAuth
    from spotify_client import SpotifyClient
    from web_app import create_web_app
    import uvicorn

    auth = SpotifyPKCEAuth(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=DEFAULT_SCOPES,
        token_cache_path=getattr(args, "token_cache", "token_cache.json"),
    )
    spotify = SpotifyClient(auth)
    cache = ArtistGenderCache(getattr(args, "cache", "artist_gender_cache.json"))
    resolver = GenderResolver(cache, user_agent=user_agent)

    host = str(getattr(args, "host", "127.0.0.1"))
    port = int(getattr(args, "port", 8890))
    web_username = (
        str(getattr(args, "web_username", "") or "").strip()
        or os.getenv("WEB_AUTH_USERNAME", "").strip()
        or "admin"
    )
    provided_web_password = (
        str(getattr(args, "web_password", "") or "").strip()
        or os.getenv("WEB_AUTH_PASSWORD", "").strip()
    )
    web_password = provided_web_password or secrets.token_urlsafe(18)
    require_auth_for_local = bool(getattr(args, "auth_all", False)) or _env_bool(
        "WEB_AUTH_ALL"
    )
    url = f"http://{host}:{port}"
    print(f"Web console: {url}")
    if require_auth_for_local:
        print("All web access requires login.")
    else:
        print("Non-local web access requires login.")
    print(f"Username: {web_username}")
    if provided_web_password:
        print("Password: read from command line or WEB_AUTH_PASSWORD")
    else:
        print(f"Temporary password: {web_password}")
    print("Press Ctrl+C to stop.")
    if not getattr(args, "no_open", False):
        webbrowser.open(url)

    app = create_web_app(
        spotify=spotify,
        resolver=resolver,
        cache=cache,
        config=config,
        should_skip_func=should_skip,
        is_liked_songs_func=_is_liked_songs_playback,
        verbose=bool(getattr(args, "verbose", False)),
        enable_skip=True,
        web_username=web_username,
        web_password=web_password,
        require_auth_for_local=require_auth_for_local,
    )

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    finally:
        resolver.close()

    return 0


def label_artist(args: argparse.Namespace) -> int:
    cache = ArtistGenderCache(args.cache)
    entry = cache.label(
        spotify_artist_id=args.spotify_artist_id,
        gender=args.gender,
        name=args.name,
        group_composition=args.group_composition,
        artist_role=args.artist_role,
    )
    group_details = (
        f", group_composition={entry['group_composition']}"
        if entry["gender"] == "group"
        else ""
    )
    print(
        f"Labeled {args.spotify_artist_id}: {entry['gender']}{group_details} "
        f"({entry['source']})"
    )
    return 0


def process_playback_once(
    spotify: SpotifyClient,
    resolver: GenderResolver,
    config: dict[str, Any],
    state: dict[str, Any],
) -> None:
    playback = spotify.get_current_playback()
    if not playback:
        print("No active Spotify playback.")
        return

    if not playback.get("is_playing"):
        print("Spotify is paused or not currently playing.")
        return

    device = playback.get("device") or {}
    if not device:
        print("No active Spotify device found.")
        return
    if device.get("is_restricted"):
        print("Current Spotify device is restricted; cannot control playback.")
        return

    disallows = (playback.get("actions") or {}).get("disallows") or {}
    if disallows.get("skipping_next"):
        print("Spotify says skipping to the next track is currently disallowed.")
        return

    if playback.get("currently_playing_type") != "track":
        print("Current Spotify item is not a song; skipping this polling cycle.")
        return

    item = playback.get("item") or {}
    if item.get("type") != "track":
        print("Current Spotify item is not a track; skipping this polling cycle.")
        return

    track_id = item.get("id")
    if not track_id:
        print("Current track has no Spotify ID; skipping this polling cycle.")
        return
    if state.get("last_track_id") == track_id:
        return

    track_name = str(item.get("name") or "Unknown track")
    album = item.get("album") or {}
    album_name = str(album.get("name") or "Unknown album")
    artists = _extract_artists(item)

    print(f"Now playing: {track_name} - {', '.join(artist['name'] for artist in artists)}")
    print(f"Album: {album_name}")

    if _is_liked_songs_playback(playback, config):
        print("Action: keep (Liked Songs source is exempt)")
        state["last_track_id"] = track_id
        return

    artist_results: list[ArtistGender] = []
    for artist in artists:
        result = resolver.resolve_artist(artist["id"], artist["name"])
        artist_results.append(result)
        print(_format_artist_result(result))

    artist_results, prompt_canceled = prompt_for_unknown_artist_labels(
        artist_results,
        resolver.cache,
        config,
        spotify=spotify,
        current_track_id=track_id,
    )
    if prompt_canceled:
        print("Action: prompt_canceled_track_changed")
        return

    should_skip_track, reason = should_skip(artist_results, config)
    if should_skip_track:
        if config.get("dry_run"):
            print(f"Action: dry_run_skip ({reason})")
        else:
            ok = spotify.skip_to_next(device_id=device.get("id"))
            print(f"Action: skip ({reason})" if ok else f"Action: skip_failed ({reason})")
    else:
        print(f"Action: keep ({reason})")

    state["last_track_id"] = track_id


def should_skip(
    artist_results: list[ArtistGender | dict[str, Any]],
    config: dict[str, Any],
) -> tuple[bool, str]:
    if not artist_results:
        return False, "no artists found"

    considered = (
        artist_results
        if bool(config.get("skip_if_any_artist_male", True))
        else artist_results[:1]
    )

    male_names = [
        _result_name(result)
        for result in considered
        if _result_gender(result) == "male"
        and not _is_protected_male_composer(result, config)
    ]
    if male_names:
        return True, f"male artist detected: {', '.join(male_names)}"

    protected_male_names = [
        _result_name(result)
        for result in considered
        if _result_gender(result) == "male"
        and _is_protected_male_composer(result, config)
    ]
    if protected_male_names:
        return False, f"protected male composer/score artist: {', '.join(protected_male_names)}"

    if bool(config.get("skip_unknown", False)):
        unknown_names = [
            _result_name(result) for result in considered if _result_gender(result) == "unknown"
        ]
        if unknown_names:
            return True, f"unknown artist gender: {', '.join(unknown_names)}"

    if bool(config.get("skip_all_male_groups", False)):
        all_male_group_names = [
            _result_name(result)
            for result in considered
            if _result_gender(result) == "group"
            and _result_group_composition(result) == "all_male"
        ]
        if all_male_group_names:
            return True, f"all-male group detected: {', '.join(all_male_group_names)}"

    if bool(config.get("skip_groups", False)):
        group_names = [
            _result_name(result) for result in considered if _result_gender(result) == "group"
        ]
        if group_names:
            return True, f"group artist detected: {', '.join(group_names)}"

    return False, "no configured skip condition matched"


def prompt_for_unknown_artist_labels(
    artist_results: list[ArtistGender],
    cache: ArtistGenderCache,
    config: dict[str, Any],
    *,
    spotify: Any | None = None,
    current_track_id: str | None = None,
) -> tuple[list[ArtistGender], bool]:
    if not bool(config.get("prompt_on_unknown", False)):
        return artist_results, False

    updated_results: list[ArtistGender] = []
    for result in artist_results:
        if result.gender == "unknown":
            updated_result, canceled = _prompt_for_unknown_gender_label(
                result,
                cache,
                spotify=spotify,
                current_track_id=current_track_id,
            )
        elif result.gender == "group" and result.group_composition == "unknown":
            updated_result, canceled = _prompt_for_unknown_group_composition(
                result,
                cache,
                spotify=spotify,
                current_track_id=current_track_id,
            )
        else:
            updated_results.append(result)
            continue

        if canceled:
            return updated_results + artist_results[len(updated_results):], True
        updated_results.append(updated_result)

    return updated_results, False


def _prompt_for_unknown_gender_label(
    result: ArtistGender,
    cache: ArtistGenderCache,
    *,
    spotify: Any | None,
    current_track_id: str | None,
) -> tuple[ArtistGender, bool]:
    print(f"Unknown artist: {result.name} ({result.spotify_artist_id})")
    raw_label, canceled = _read_runtime_label(
        "Label now? [male/female/other/group/unknown, Enter=leave unknown]: ",
        spotify=spotify,
        current_track_id=current_track_id,
    )
    if canceled:
        return result, True
    if raw_label is None:
        print("No interactive input available; leaving artist as unknown.")
        return result, False

    gender = _normalize_prompt_gender(raw_label)
    if gender is None:
        print("No label saved.")
        return result, False

    entry = cache.label(result.spotify_artist_id, gender, name=result.name)
    updated = ArtistGender(
        spotify_artist_id=result.spotify_artist_id,
        name=result.name,
        gender=str(entry["gender"]),
        source="manual",
        confidence=float(entry["confidence"]),
        group_composition=str(entry["group_composition"]),
        artist_role=str(entry["artist_role"]),
    )
    print(f"Runtime label saved: {_format_artist_result(updated)}")
    return updated, False


def _prompt_for_unknown_group_composition(
    result: ArtistGender,
    cache: ArtistGenderCache,
    *,
    spotify: Any | None,
    current_track_id: str | None,
) -> tuple[ArtistGender, bool]:
    print(f"Group composition unknown: {result.name} ({result.spotify_artist_id})")
    raw_label, canceled = _read_runtime_label(
        "Label group composition? [all_male/all_female/mixed/all_other/unknown, "
        "Enter=leave unknown]: ",
        spotify=spotify,
        current_track_id=current_track_id,
    )
    if canceled:
        return result, True
    if raw_label is None:
        print("No interactive input available; leaving group composition as unknown.")
        return result, False

    group_composition = _normalize_prompt_group_composition(raw_label)
    if group_composition is None:
        print("No group composition label saved.")
        return result, False

    entry = cache.label(
        result.spotify_artist_id,
        "group",
        name=result.name,
        group_composition=group_composition,
    )
    updated = ArtistGender(
        spotify_artist_id=result.spotify_artist_id,
        name=result.name,
        gender="group",
        source="manual",
        confidence=float(entry["confidence"]),
        group_composition=str(entry["group_composition"]),
        artist_role=str(entry["artist_role"]),
    )
    print(f"Runtime group composition saved: {_format_artist_result(updated)}")
    return updated, False


def _read_runtime_label(
    prompt_text: str,
    *,
    spotify: Any | None,
    current_track_id: str | None,
) -> tuple[str | None, bool]:
    if spotify is not None and current_track_id and os.name == "nt" and sys.stdin.isatty():
        return _read_windows_runtime_label(
            prompt_text,
            spotify=spotify,
            current_track_id=current_track_id,
        )

    try:
        return input(prompt_text), False
    except EOFError:
        return None, False


def _read_windows_runtime_label(
    prompt_text: str,
    *,
    spotify: Any,
    current_track_id: str,
    check_interval_seconds: float = 1.0,
) -> tuple[str | None, bool]:
    import msvcrt

    chars: list[str] = []
    last_check_at = 0.0
    print(prompt_text, end="", flush=True)

    while True:
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            if char in ("\r", "\n"):
                print()
                return "".join(chars), False
            if char == "\b":
                if chars:
                    chars.pop()
                    print("\b \b", end="", flush=True)
                continue
            chars.append(char)
            print(char, end="", flush=True)

        now = time.monotonic()
        if now - last_check_at >= check_interval_seconds:
            last_check_at = now
            if _spotify_left_track(spotify, current_track_id):
                print("\nPrompt canceled because Spotify moved to another track.")
                return None, True

        time.sleep(0.05)


def _spotify_left_track(spotify: Any, current_track_id: str) -> bool:
    playback = spotify.get_current_playback()
    if not playback:
        return False

    if playback.get("currently_playing_type") != "track":
        return True

    item = playback.get("item") or {}
    latest_track_id = item.get("id")
    return bool(latest_track_id and latest_track_id != current_track_id)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        example = Path(__file__).with_name("config.example.json")
        if example.exists():
            shutil.copyfile(example, path)
        else:
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"Created default config at {path}.")

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {path}: {exc}")
        print("Using built-in default config for this run.")
        loaded = {}

    config = dict(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        config.update(loaded)

    try:
        config["poll_interval_seconds"] = max(1, float(config["poll_interval_seconds"]))
    except (TypeError, ValueError):
        config["poll_interval_seconds"] = DEFAULT_CONFIG["poll_interval_seconds"]

    try:
        config["queue_prefetch_tracks"] = max(0, min(10, int(config["queue_prefetch_tracks"])))
    except (TypeError, ValueError):
        config["queue_prefetch_tracks"] = DEFAULT_CONFIG["queue_prefetch_tracks"]

    try:
        config["smart_skip_max_tracks"] = max(1, min(25, int(config["smart_skip_max_tracks"])))
    except (TypeError, ValueError):
        config["smart_skip_max_tracks"] = DEFAULT_CONFIG["smart_skip_max_tracks"]

    for key in (
        "skip_if_any_artist_male",
        "skip_unknown",
        "skip_groups",
        "skip_all_male_groups",
        "keep_male_composers",
        "keep_liked_songs",
        "prompt_on_unknown",
        "dry_run",
    ):
        config[key] = bool(config.get(key, DEFAULT_CONFIG[key]))

    return config


def print_first_run_setup() -> None:
    print("Spotify setup is needed before the first run.")
    print("1. Open https://developer.spotify.com/dashboard and create an app.")
    print(f"2. Add this Redirect URI exactly: {DEFAULT_REDIRECT_URI}")
    print("3. Copy your Spotify Client ID into a local .env file:")
    print("   SPOTIFY_CLIENT_ID=your_client_id_here")
    print("4. Run again with: python main.py")
    print("No client secret is needed because this app uses Authorization Code with PKCE.")


def _env_bool(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _extract_artists(item: dict[str, Any]) -> list[dict[str, str]]:
    artists: list[dict[str, str]] = []
    for artist in item.get("artists") or []:
        spotify_id = artist.get("id")
        name = artist.get("name")
        if spotify_id and name:
            artists.append({"id": str(spotify_id), "name": str(name)})
    return artists


def _is_liked_songs_playback(playback: dict[str, Any], config: dict[str, Any]) -> bool:
    if not bool(config.get("keep_liked_songs", True)):
        return False

    context = playback.get("context")
    if not isinstance(context, dict):
        return False

    context_type = str(context.get("type") or "").strip().lower()
    context_href = str(context.get("href") or "").strip().lower().rstrip("/")
    context_uri = str(context.get("uri") or "").strip().lower()

    return (
        context_type == "collection"
        or context_href.endswith("/me/tracks")
        or context_uri.endswith(":collection")
        or context_uri == "spotify:collection:tracks"
    )


def _result_gender(result: ArtistGender | dict[str, Any]) -> str:
    if isinstance(result, ArtistGender):
        return result.gender
    return str(result.get("gender") or "unknown")


def _result_name(result: ArtistGender | dict[str, Any]) -> str:
    if isinstance(result, ArtistGender):
        return result.name
    return str(result.get("name") or result.get("spotify_artist_id") or "unknown")


def _result_group_composition(result: ArtistGender | dict[str, Any]) -> str:
    if isinstance(result, ArtistGender):
        return result.group_composition
    return normalize_group_composition(str(result.get("group_composition") or "unknown"))


def _result_artist_role(result: ArtistGender | dict[str, Any]) -> str:
    if isinstance(result, ArtistGender):
        return result.artist_role
    return normalize_artist_role(str(result.get("artist_role") or "unknown"))


def _is_protected_male_composer(
    result: ArtistGender | dict[str, Any],
    config: dict[str, Any],
) -> bool:
    return bool(config.get("keep_male_composers", True)) and (
        _result_artist_role(result) == "composer_or_score"
    )


def _format_artist_result(result: ArtistGender) -> str:
    group_details = (
        f", group_composition={result.group_composition}" if result.gender == "group" else ""
    )
    role_details = (
        f", artist_role={result.artist_role}" if result.artist_role != "unknown" else ""
    )
    return (
        f"{result.name} => {result.gender}{group_details}{role_details}, "
        f"confidence={result.confidence:.2f}, source={result.source}, "
        f"spotify_artist_id={result.spotify_artist_id}"
    )


def _normalize_prompt_gender(raw_label: str) -> str | None:
    label = raw_label.strip().lower()
    if not label:
        return None

    aliases = {
        "m": "male",
        "f": "female",
        "o": "other",
        "g": "group",
        "u": "unknown",
    }
    gender = normalize_gender(aliases.get(label, label))
    return gender if gender in VALID_GENDERS else None


def _normalize_prompt_group_composition(raw_label: str) -> str | None:
    label = raw_label.strip().lower().replace("-", "_").replace(" ", "_")
    if not label:
        return None

    aliases = {
        "m": "all_male",
        "male": "all_male",
        "allmale": "all_male",
        "f": "all_female",
        "female": "all_female",
        "allfemale": "all_female",
        "mix": "mixed",
        "mixed": "mixed",
        "o": "all_other",
        "other": "all_other",
        "allother": "all_other",
        "u": "unknown",
        "unknown": "unknown",
    }
    group_composition = normalize_group_composition(aliases.get(label, label))
    valid_values = VALID_GROUP_COMPOSITIONS - {"not_group"}
    return group_composition if group_composition in valid_values else None


if __name__ == "__main__":
    sys.exit(main())
