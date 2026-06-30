from __future__ import annotations

import json
import threading
import time

from spotify_auth import SpotifyPKCEAuth


def test_concurrent_access_token_requests_share_single_interactive_login(tmp_path, monkeypatch) -> None:
    auth = SpotifyPKCEAuth(
        client_id="client-id",
        token_cache_path=tmp_path / "token_cache.json",
    )
    login_calls = 0
    login_lock = threading.Lock()

    def fake_authorize():
        nonlocal login_calls
        with login_lock:
            login_calls += 1
        time.sleep(0.05)
        token = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        auth._save_token(token)
        return token

    monkeypatch.setattr(auth, "_authorize_interactively", fake_authorize)

    results: list[str] = []

    def worker() -> None:
        results.append(auth.get_access_token())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["access-token"] * 4
    assert login_calls == 1


def test_missing_required_scope_forces_interactive_login(tmp_path, monkeypatch) -> None:
    token_cache_path = tmp_path / "token_cache.json"
    token_cache_path.write_text(
        json.dumps(
            {
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "expires_at": time.time() + 3600,
                "scope": "user-read-currently-playing user-read-playback-state user-modify-playback-state",
            }
        ),
        encoding="utf-8",
    )
    auth = SpotifyPKCEAuth(
        client_id="client-id",
        token_cache_path=token_cache_path,
    )
    login_calls = 0

    def fail_refresh(refresh_token: str):
        raise AssertionError("old refresh token should not be used when scopes are missing")

    def fake_authorize():
        nonlocal login_calls
        login_calls += 1
        token = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_at": time.time() + 3600,
            "scope": " ".join(auth.scopes),
        }
        auth._save_token(token)
        return token

    monkeypatch.setattr(auth, "_refresh_token", fail_refresh)
    monkeypatch.setattr(auth, "_authorize_interactively", fake_authorize)

    assert auth.get_access_token() == "new-access-token"
    saved_token = json.loads(token_cache_path.read_text(encoding="utf-8"))
    assert login_calls == 1
    assert "user-library-read" in saved_token["requested_scopes"]
    assert "user-library-modify" in saved_token["requested_scopes"]
