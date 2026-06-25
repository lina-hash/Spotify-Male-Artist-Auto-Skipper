from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VALID_GENDERS = {"male", "female", "other", "group", "unknown"}
VALID_GROUP_COMPOSITIONS = {
    "not_group",
    "all_male",
    "all_female",
    "mixed",
    "all_other",
    "unknown",
}
GENDER_ALIASES = {
    "nonbinary": "other",
    "non-binary": "other",
}
GROUP_COMPOSITION_ALIASES = {
    "male": "all_male",
    "female": "all_female",
    "other": "all_other",
    "all-male": "all_male",
    "all-female": "all_female",
    "all-other": "all_other",
}


def normalize_gender(gender: str) -> str:
    normalized = gender.strip().lower()
    return GENDER_ALIASES.get(normalized, normalized)


def normalize_group_composition(group_composition: str) -> str:
    normalized = group_composition.strip().lower()
    return GROUP_COMPOSITION_ALIASES.get(normalized, normalized)


class ArtistGenderCache:
    def __init__(self, path: str | Path = "artist_gender_cache.json") -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read cache {self.path}: {exc}")
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def get(self, spotify_artist_id: str) -> dict[str, Any] | None:
        entry = self._data.get(spotify_artist_id)
        return dict(entry) if isinstance(entry, dict) else None

    def set(
        self,
        spotify_artist_id: str,
        name: str,
        gender: str,
        source: str,
        confidence: float,
        group_composition: str | None = None,
    ) -> dict[str, Any]:
        gender = normalize_gender(gender)
        if gender not in VALID_GENDERS:
            raise ValueError(f"Invalid gender '{gender}'. Expected one of {sorted(VALID_GENDERS)}")
        if group_composition is None:
            group_composition = "unknown" if gender == "group" else "not_group"
        group_composition = normalize_group_composition(group_composition)
        if gender != "group":
            group_composition = "not_group"
        elif group_composition == "not_group":
            group_composition = "unknown"
        if group_composition not in VALID_GROUP_COMPOSITIONS:
            raise ValueError(
                f"Invalid group composition '{group_composition}'. "
                f"Expected one of {sorted(VALID_GROUP_COMPOSITIONS)}"
            )

        entry = {
            "name": name,
            "gender": gender,
            "group_composition": group_composition,
            "source": source,
            "confidence": max(0.0, min(1.0, float(confidence))),
        }
        self._data[spotify_artist_id] = entry
        self.save()
        return dict(entry)

    def label(
        self,
        spotify_artist_id: str,
        gender: str,
        name: str | None = None,
        group_composition: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get(spotify_artist_id) or {}
        label_name = name or str(existing.get("name") or spotify_artist_id)
        if group_composition is None:
            group_composition = str(existing.get("group_composition") or "")
            if not group_composition:
                group_composition = "unknown" if normalize_gender(gender) == "group" else "not_group"
        return self.set(
            spotify_artist_id=spotify_artist_id,
            name=label_name,
            gender=gender,
            source="manual",
            confidence=1.0,
            group_composition=group_composition,
        )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)
