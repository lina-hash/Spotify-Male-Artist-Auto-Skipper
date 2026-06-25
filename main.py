from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False

from cache import (
    ArtistGenderCache,
    VALID_GENDERS,
    VALID_GROUP_COMPOSITIONS,
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
]


DEFAULT_CONFIG = {
    "poll_interval_seconds": 3,
    "skip_if_any_artist_male": True,
    "skip_unknown": False,
    "skip_groups": False,
    "skip_all_male_groups": False,
    "prompt_on_unknown": False,
    "dry_run": False,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "label":
        return label_artist(args)

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


def label_artist(args: argparse.Namespace) -> int:
    cache = ArtistGenderCache(args.cache)
    entry = cache.label(
        spotify_artist_id=args.spotify_artist_id,
        gender=args.gender,
        name=args.name,
        group_composition=args.group_composition,
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

    male_names = [_result_name(result) for result in considered if _result_gender(result) == "male"]
    if male_names:
        return True, f"male artist detected: {', '.join(male_names)}"

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

    for key in (
        "skip_if_any_artist_male",
        "skip_unknown",
        "skip_groups",
        "skip_all_male_groups",
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


def _extract_artists(item: dict[str, Any]) -> list[dict[str, str]]:
    artists: list[dict[str, str]] = []
    for artist in item.get("artists") or []:
        spotify_id = artist.get("id")
        name = artist.get("name")
        if spotify_id and name:
            artists.append({"id": str(spotify_id), "name": str(name)})
    return artists


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


def _format_artist_result(result: ArtistGender) -> str:
    group_details = (
        f", group_composition={result.group_composition}" if result.gender == "group" else ""
    )
    return (
        f"{result.name} => {result.gender}{group_details}, "
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
