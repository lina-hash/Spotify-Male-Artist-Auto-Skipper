from __future__ import annotations

from cache import ArtistGenderCache
from gender_resolver import ArtistGender
from main import DEFAULT_CONFIG, prompt_for_unknown_artist_labels, should_skip


def artist(name: str, gender: str, group_composition: str = "not_group") -> ArtistGender:
    return ArtistGender(
        spotify_artist_id=name.lower().replace(" ", "-"),
        name=name,
        gender=gender,
        source="test",
        confidence=1.0,
        group_composition=group_composition,
    )


def test_skip_if_any_artist_male() -> None:
    config = dict(DEFAULT_CONFIG)
    config["skip_if_any_artist_male"] = True

    should_skip_track, reason = should_skip(
        [artist("Main Artist", "female"), artist("Featured Artist", "male")],
        config,
    )

    assert should_skip_track is True
    assert "Featured Artist" in reason


def test_only_main_artist_when_configured() -> None:
    config = dict(DEFAULT_CONFIG)
    config["skip_if_any_artist_male"] = False

    should_skip_track, reason = should_skip(
        [artist("Main Artist", "female"), artist("Featured Artist", "male")],
        config,
    )

    assert should_skip_track is False
    assert "no configured skip condition" in reason


def test_skip_unknown_when_enabled() -> None:
    config = dict(DEFAULT_CONFIG)
    config["skip_unknown"] = True

    should_skip_track, reason = should_skip([artist("Mystery Artist", "unknown")], config)

    assert should_skip_track is True
    assert "unknown" in reason


def test_groups_are_not_skipped_by_default() -> None:
    config = dict(DEFAULT_CONFIG)

    should_skip_track, reason = should_skip([artist("Band C", "group")], config)

    assert should_skip_track is False
    assert "no configured skip condition" in reason


def test_groups_can_be_skipped() -> None:
    config = dict(DEFAULT_CONFIG)
    config["skip_groups"] = True

    should_skip_track, reason = should_skip([artist("Band C", "group")], config)

    assert should_skip_track is True
    assert "Band C" in reason


def test_all_male_groups_are_not_skipped_by_default() -> None:
    config = dict(DEFAULT_CONFIG)

    should_skip_track, reason = should_skip([artist("Band D", "group", "all_male")], config)

    assert should_skip_track is False
    assert "no configured skip condition" in reason


def test_all_male_groups_can_be_skipped() -> None:
    config = dict(DEFAULT_CONFIG)
    config["skip_all_male_groups"] = True

    should_skip_track, reason = should_skip([artist("Band D", "group", "all_male")], config)

    assert should_skip_track is True
    assert "Band D" in reason


def test_runtime_prompt_can_label_unknown_as_male(tmp_path, monkeypatch) -> None:
    config = dict(DEFAULT_CONFIG)
    config["prompt_on_unknown"] = True
    cache = ArtistGenderCache(tmp_path / "cache.json")
    unknown = artist("Mystery Artist", "unknown")

    monkeypatch.setattr("builtins.input", lambda prompt: "male")

    updated_results, canceled = prompt_for_unknown_artist_labels([unknown], cache, config)
    should_skip_track, reason = should_skip(updated_results, config)

    assert canceled is False
    assert updated_results[0].gender == "male"
    assert updated_results[0].source == "manual"
    assert cache.get(unknown.spotify_artist_id)["gender"] == "male"
    assert should_skip_track is True
    assert "Mystery Artist" in reason


def test_runtime_prompt_can_label_unknown_group_composition(tmp_path, monkeypatch) -> None:
    config = dict(DEFAULT_CONFIG)
    config["prompt_on_unknown"] = True
    config["skip_all_male_groups"] = True
    cache = ArtistGenderCache(tmp_path / "cache.json")
    unknown_group = artist("Mystery Band", "group", "unknown")

    monkeypatch.setattr("builtins.input", lambda prompt: "all_male")

    updated_results, canceled = prompt_for_unknown_artist_labels(
        [unknown_group],
        cache,
        config,
    )
    should_skip_track, reason = should_skip(updated_results, config)

    assert canceled is False
    assert updated_results[0].gender == "group"
    assert updated_results[0].group_composition == "all_male"
    assert updated_results[0].source == "manual"
    assert cache.get(unknown_group.spotify_artist_id)["group_composition"] == "all_male"
    assert should_skip_track is True
    assert "Mystery Band" in reason


def test_runtime_prompt_can_cancel_when_track_changes(tmp_path, monkeypatch) -> None:
    config = dict(DEFAULT_CONFIG)
    config["prompt_on_unknown"] = True
    cache = ArtistGenderCache(tmp_path / "cache.json")
    unknown = artist("Mystery Artist", "unknown")

    def fake_read_runtime_label(*args, **kwargs):
        return None, True

    monkeypatch.setattr("main._read_runtime_label", fake_read_runtime_label)

    updated_results, canceled = prompt_for_unknown_artist_labels(
        [unknown],
        cache,
        config,
        spotify=object(),
        current_track_id="old-track",
    )

    assert canceled is True
    assert updated_results[0].gender == "unknown"
    assert cache.get(unknown.spotify_artist_id) is None
