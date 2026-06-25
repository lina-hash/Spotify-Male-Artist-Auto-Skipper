from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import httpx

from cache import (
    VALID_GROUP_COMPOSITIONS,
    ArtistGenderCache,
    normalize_gender,
    normalize_group_composition,
)


DEFAULT_MUSICBRAINZ_USER_AGENT = (
    "SpotifyMaleArtistAutoSkipper/1.0 "
    "(local-use; set MUSICBRAINZ_USER_AGENT for contact)"
)

UNKNOWN_GENDER = "unknown"
CLEAR_GENDERS = {"male", "female", "other", "group"}

MALE_QID = "Q6581097"
FEMALE_QID = "Q6581072"
HUMAN_QID = "Q5"
MUSIC_GROUP_QIDS = {
    "Q215380",  # musical group
    "Q5741069",  # rock band
    "Q2088357",  # musical ensemble
    "Q216337",  # boy band
    "Q641066",  # girl group
}
ALL_MALE_GROUP_QIDS = {"Q216337"}
ALL_FEMALE_GROUP_QIDS = {"Q641066"}
GROUP_MEMBER_PROPERTY_IDS = ("P527",)
MUSICBRAINZ_GROUP_MEMBER_RELATION_TYPES = {
    "member of band",
    "member of",
}
MUSIC_OCCUPATION_QIDS = {
    "Q177220",  # singer
    "Q639669",  # musician
    "Q36834",  # composer
    "Q753110",  # songwriter
    "Q2252262",  # rapper
    "Q183945",  # record producer
    "Q488205",  # singer-songwriter
    "Q855091",  # vocalist
}
MUSIC_DESCRIPTION_TERMS = {
    "singer",
    "musician",
    "rapper",
    "songwriter",
    "composer",
    "vocalist",
    "dj",
    "record producer",
    "musical artist",
    "band",
    "boy band",
    "girl group",
    "k-pop group",
    "kpop group",
}
GROUP_DESCRIPTION_TERMS = {
    "band",
    "boy band",
    "girl group",
    "music group",
    "musical group",
    "k-pop group",
    "kpop group",
}


@dataclass(frozen=True)
class ArtistGender:
    spotify_artist_id: str
    name: str
    gender: str
    source: str
    confidence: float
    group_composition: str = "not_group"


@dataclass(frozen=True)
class WikidataCandidate:
    entity_id: str
    label: str
    description: str
    search_rank: int
    gender: str
    is_human: bool
    is_group: bool
    is_music_related: bool
    group_composition: str = "not_group"

    @property
    def normalized_label(self) -> str:
        return _normalize_name(self.label)


class GenderResolver:
    def __init__(
        self,
        cache: ArtistGenderCache,
        *,
        user_agent: str = DEFAULT_MUSICBRAINZ_USER_AGENT,
        base_url: str = "https://musicbrainz.org/ws/2",
        wikidata_api_url: str = "https://www.wikidata.org/w/api.php",
        wikidata_sparql_url: str = "https://query.wikidata.org/sparql",
        min_request_interval_seconds: float = 1.2,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache = cache
        self.user_agent = user_agent.strip() or DEFAULT_MUSICBRAINZ_USER_AGENT
        self.base_url = base_url.rstrip("/")
        self.wikidata_api_url = wikidata_api_url
        self.wikidata_sparql_url = wikidata_sparql_url
        self.min_request_interval_seconds = min_request_interval_seconds
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        self._last_musicbrainz_request_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def resolve_artist(self, spotify_artist_id: str, name: str) -> ArtistGender:
        cached = self.cache.get(spotify_artist_id)
        if cached:
            cached_gender = normalize_gender(str(cached.get("gender") or UNKNOWN_GENDER))
            has_group_composition = "group_composition" in cached
            if cached_gender == "group" and not has_group_composition:
                cached = None
            else:
                return self._artist_gender_from_cache(spotify_artist_id, name, cached, cached_gender)

        result = self._resolve_uncached(spotify_artist_id, name)
        self.cache.set(
            spotify_artist_id=spotify_artist_id,
            name=name,
            gender=result.gender,
            source=result.source,
            confidence=result.confidence,
            group_composition=result.group_composition,
        )
        return result

    def _artist_gender_from_cache(
        self,
        spotify_artist_id: str,
        name: str,
        cached: dict[str, Any],
        cached_gender: str,
    ) -> ArtistGender:
            cached_group_composition = normalize_group_composition(
                str(cached.get("group_composition") or "unknown")
            )
            if cached_group_composition not in VALID_GROUP_COMPOSITIONS:
                cached_group_composition = "unknown"
            return ArtistGender(
                spotify_artist_id=spotify_artist_id,
                name=str(cached.get("name") or name),
                gender=cached_gender,
                source=str(cached.get("source") or "cache"),
                confidence=float(cached.get("confidence") or 0.0),
                group_composition=cached_group_composition
                if cached_gender == "group"
                else "not_group",
            )

    def _resolve_uncached(self, spotify_artist_id: str, name: str) -> ArtistGender:
        musicbrainz_result = self._resolve_with_musicbrainz(spotify_artist_id, name)
        if musicbrainz_result.gender == "group":
            if musicbrainz_result.group_composition != UNKNOWN_GENDER:
                return musicbrainz_result
            wikidata_result = self._resolve_with_wikidata(
                spotify_artist_id, name, prefer_group=True
            )
            if (
                wikidata_result.gender == "group"
                and wikidata_result.group_composition != "unknown"
            ):
                return wikidata_result
            return musicbrainz_result

        if musicbrainz_result.gender in CLEAR_GENDERS:
            return musicbrainz_result

        wikidata_result = self._resolve_with_wikidata(spotify_artist_id, name)
        if wikidata_result.gender in CLEAR_GENDERS:
            return wikidata_result

        return ArtistGender(
            spotify_artist_id=spotify_artist_id,
            name=name,
            gender=UNKNOWN_GENDER,
            source=wikidata_result.source,
            confidence=0.0,
            group_composition="not_group",
        )

    def _resolve_with_musicbrainz(self, spotify_artist_id: str, name: str) -> ArtistGender:
        try:
            artists = self._query_musicbrainz(name)
            gender, confidence, group_composition, musicbrainz_artist_id = (
                choose_musicbrainz_classification(name, artists)
            )
            if gender == "group" and musicbrainz_artist_id:
                relations = self._lookup_musicbrainz_artist_relations(musicbrainz_artist_id)
                relation_group_composition = choose_musicbrainz_group_composition(relations)
                if relation_group_composition != UNKNOWN_GENDER:
                    group_composition = relation_group_composition
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            print(f"MusicBrainz lookup failed for {name}: {exc}")
            gender, confidence, group_composition = UNKNOWN_GENDER, 0.0, "not_group"

        return ArtistGender(
            spotify_artist_id=spotify_artist_id,
            name=name,
            gender=gender,
            source="musicbrainz",
            confidence=confidence,
            group_composition=group_composition if gender == "group" else "not_group",
        )

    def _query_musicbrainz(self, name: str) -> list[dict[str, Any]]:
        self._respect_musicbrainz_rate_limit()

        response = self.client.get(
            f"{self.base_url}/artist",
            params={"query": name, "fmt": "json", "limit": "5"},
            headers=self._headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

        if response.status_code == 429:
            raise RuntimeError("MusicBrainz rate limit reached")

        response.raise_for_status()
        payload = response.json()
        artists = payload.get("artists", [])
        return artists if isinstance(artists, list) else []

    def _lookup_musicbrainz_artist_relations(
        self, musicbrainz_artist_id: str
    ) -> list[dict[str, Any]]:
        self._respect_musicbrainz_rate_limit()

        response = self.client.get(
            f"{self.base_url}/artist/{musicbrainz_artist_id}",
            params={"inc": "artist-rels", "fmt": "json"},
            headers=self._headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

        if response.status_code == 429:
            raise RuntimeError("MusicBrainz rate limit reached")

        response.raise_for_status()
        payload = response.json()
        relations = payload.get("relations", [])
        return relations if isinstance(relations, list) else []

    def _resolve_with_wikidata(
        self, spotify_artist_id: str, name: str, *, prefer_group: bool = False
    ) -> ArtistGender:
        try:
            search_results = self._search_wikidata(name)
            candidates = self._build_wikidata_candidates(search_results)
            gender, confidence, group_composition = choose_wikidata_classification(
                name, candidates, prefer_group=prefer_group
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            print(f"Wikidata lookup failed for {name}: {exc}")
            gender, confidence, group_composition = UNKNOWN_GENDER, 0.0, "not_group"

        return ArtistGender(
            spotify_artist_id=spotify_artist_id,
            name=name,
            gender=gender,
            source="wikidata",
            confidence=confidence,
            group_composition=group_composition,
        )

    def _search_wikidata(self, name: str) -> list[dict[str, Any]]:
        response = self.client.get(
            self.wikidata_api_url,
            params={
                "action": "wbsearchentities",
                "search": name,
                "language": "en",
                "format": "json",
                "type": "item",
                "limit": "5",
            },
            headers=self._headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        response.raise_for_status()
        payload = response.json()
        search_results = payload.get("search", [])
        return search_results if isinstance(search_results, list) else []

    def _build_wikidata_candidates(
        self, search_results: list[dict[str, Any]]
    ) -> list[WikidataCandidate]:
        entity_ids = [
            str(result.get("id"))
            for result in search_results
            if isinstance(result.get("id"), str) and str(result.get("id")).startswith("Q")
        ]
        if not entity_ids:
            return []

        response = self.client.get(
            self.wikidata_api_url,
            params={
                "action": "wbgetentities",
                "ids": "|".join(entity_ids),
                "props": "claims|labels|descriptions",
                "languages": "en",
                "format": "json",
            },
            headers=self._headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        response.raise_for_status()
        payload = response.json()
        entities = payload.get("entities", {})
        if not isinstance(entities, dict):
            return []

        candidate_parts: list[dict[str, Any]] = []
        all_member_qids: set[str] = set()
        for rank, result in enumerate(search_results):
            entity_id = str(result.get("id") or "")
            entity = entities.get(entity_id)
            if not isinstance(entity, dict):
                continue

            label = _wikidata_text(entity, "labels") or str(result.get("label") or "")
            description = _wikidata_text(entity, "descriptions") or str(
                result.get("description") or ""
            )
            claims = entity.get("claims") if isinstance(entity.get("claims"), dict) else {}
            instance_of = _claim_qids(claims, "P31")
            occupations = _claim_qids(claims, "P106")
            member_qids = _group_member_qids(claims)
            all_member_qids.update(member_qids)
            is_group = bool(instance_of & MUSIC_GROUP_QIDS) or _description_is_group(description)

            candidate_parts.append(
                {
                    "entity_id": entity_id,
                    "label": label,
                    "description": description,
                    "search_rank": rank,
                    "gender": _wikidata_gender(_claim_qids(claims, "P21")),
                    "is_human": HUMAN_QID in instance_of,
                    "is_group": is_group,
                    "is_music_related": bool(occupations & MUSIC_OCCUPATION_QIDS)
                    or _description_is_music_related(description),
                    "member_qids": member_qids,
                    "group_hint_composition": _group_composition_from_wikidata_hints(
                        instance_of, description
                    ),
                }
            )

        member_gender_by_qid = self._fetch_wikidata_member_genders(all_member_qids)
        group_entity_ids = {
            str(part["entity_id"]) for part in candidate_parts if bool(part["is_group"])
        }
        p463_member_genders_by_group = self._fetch_wikidata_p463_member_genders(
            group_entity_ids
        )
        candidates: list[WikidataCandidate] = []
        for part in candidate_parts:
            member_genders = [
                member_gender_by_qid.get(member_qid, UNKNOWN_GENDER)
                for member_qid in part["member_qids"]
            ]
            member_genders.extend(
                p463_member_genders_by_group.get(str(part["entity_id"]), [])
            )
            group_composition = _group_composition_from_member_genders(member_genders)
            if group_composition == UNKNOWN_GENDER:
                group_composition = str(part["group_hint_composition"])
            candidates.append(
                WikidataCandidate(
                    entity_id=part["entity_id"],
                    label=part["label"],
                    description=part["description"],
                    search_rank=part["search_rank"],
                    gender=part["gender"],
                    is_human=part["is_human"],
                    is_group=part["is_group"],
                    is_music_related=part["is_music_related"],
                    group_composition=group_composition
                    if part["is_group"]
                    else "not_group",
                )
            )

        return candidates

    def _fetch_wikidata_member_genders(self, member_qids: set[str]) -> dict[str, str]:
        member_qids = {qid for qid in member_qids if qid.startswith("Q")}
        if not member_qids:
            return {}

        limited_qids = sorted(member_qids)[:50]
        response = self.client.get(
            self.wikidata_api_url,
            params={
                "action": "wbgetentities",
                "ids": "|".join(limited_qids),
                "props": "claims",
                "languages": "en",
                "format": "json",
            },
            headers=self._headers(),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        response.raise_for_status()
        payload = response.json()
        entities = payload.get("entities", {})
        if not isinstance(entities, dict):
            return {}

        member_genders: dict[str, str] = {}
        for qid in limited_qids:
            entity = entities.get(qid)
            if not isinstance(entity, dict):
                member_genders[qid] = UNKNOWN_GENDER
                continue
            claims = entity.get("claims") if isinstance(entity.get("claims"), dict) else {}
            member_genders[qid] = _wikidata_gender(_claim_qids(claims, "P21"))
        return member_genders

    def _fetch_wikidata_p463_member_genders(self, group_qids: set[str]) -> dict[str, list[str]]:
        group_qids = {qid for qid in group_qids if qid.startswith("Q")}
        if not group_qids:
            return {}

        values = " ".join(f"wd:{qid}" for qid in sorted(group_qids)[:20])
        query = f"""
SELECT ?group ?member ?gender WHERE {{
  VALUES ?group {{ {values} }}
  ?member wdt:P463 ?group .
  ?member wdt:P31 wd:Q5 .
  OPTIONAL {{ ?member wdt:P21 ?gender. }}
}}
LIMIT 100
""".strip()

        try:
            response = self.client.get(
                self.wikidata_sparql_url,
                params={"query": query, "format": "json"},
                headers=self._headers(),
                timeout=httpx.Timeout(12.0, connect=5.0),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"Wikidata P463 member lookup failed: {exc}")
            return {}

        bindings = ((payload.get("results") or {}).get("bindings") or [])
        if not isinstance(bindings, list):
            return {}

        genders_by_group: dict[str, list[str]] = {qid: [] for qid in group_qids}
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            group_qid = _qid_from_wikidata_uri(
                ((binding.get("group") or {}).get("value") or "")
            )
            if not group_qid:
                continue
            gender_qid = _qid_from_wikidata_uri(
                ((binding.get("gender") or {}).get("value") or "")
            )
            genders_by_group.setdefault(group_qid, []).append(
                _wikidata_gender({gender_qid} if gender_qid else set())
            )

        return genders_by_group

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def _respect_musicbrainz_rate_limit(self) -> None:
        if self.min_request_interval_seconds <= 0:
            self._last_musicbrainz_request_at = time.monotonic()
            return

        elapsed = time.monotonic() - self._last_musicbrainz_request_at
        wait_for = self.min_request_interval_seconds - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_musicbrainz_request_at = time.monotonic()


def choose_musicbrainz_gender(name: str, artists: list[dict[str, Any]]) -> tuple[str, float]:
    gender, confidence, _group_composition, _musicbrainz_artist_id = (
        choose_musicbrainz_classification(name, artists)
    )
    return gender, confidence


def choose_musicbrainz_classification(
    name: str, artists: list[dict[str, Any]]
) -> tuple[str, float, str, str | None]:
    candidates = [_candidate_from_musicbrainz_artist(artist) for artist in artists]
    candidates = [candidate for candidate in candidates if candidate["score"] >= 70]

    if not candidates:
        return UNKNOWN_GENDER, 0.0, "not_group", None

    target_name = _normalize_name(name)
    exact_matches = [
        candidate for candidate in candidates if _normalize_name(candidate["name"]) == target_name
    ]

    if exact_matches:
        return _choose_musicbrainz_pool(exact_matches, exact=True)

    sorted_candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
    max_score = sorted_candidates[0]["score"]
    if max_score < 85:
        return UNKNOWN_GENDER, round(max_score / 200, 2), "not_group", None

    top_pool = [
        candidate for candidate in sorted_candidates if candidate["score"] >= max_score - 3
    ]
    return _choose_musicbrainz_pool(top_pool, exact=False)


def choose_wikidata_gender(
    name: str, candidates: list[WikidataCandidate]
) -> tuple[str, float]:
    gender, confidence, _group_composition = choose_wikidata_classification(name, candidates)
    return gender, confidence


def choose_wikidata_classification(
    name: str, candidates: list[WikidataCandidate], *, prefer_group: bool = False
) -> tuple[str, float, str]:
    target_name = _normalize_name(name)

    exact_groups = [
        candidate
        for candidate in candidates
        if candidate.normalized_label == target_name and candidate.is_group
    ]
    exact_music_people = [
        candidate
        for candidate in candidates
        if (
            candidate.normalized_label == target_name
            and candidate.is_human
            and candidate.is_music_related
        )
    ]

    if prefer_group and exact_groups:
        return _choose_wikidata_pool(exact_groups, confidence=0.8)

    if exact_groups and exact_music_people:
        return UNKNOWN_GENDER, 0.0, "not_group"

    if exact_music_people:
        return _choose_wikidata_pool(exact_music_people, confidence=0.82)

    if exact_groups:
        return _choose_wikidata_pool(exact_groups, confidence=0.8)

    fuzzy_music_people = [
        candidate
        for candidate in candidates
        if candidate.is_human
        and candidate.is_music_related
        and _name_similarity(name, candidate.label) >= 0.92
    ]
    if fuzzy_music_people:
        return _choose_wikidata_pool(fuzzy_music_people, confidence=0.68)

    fuzzy_groups = [
        candidate
        for candidate in candidates
        if candidate.is_group and _name_similarity(name, candidate.label) >= 0.92
    ]
    if prefer_group and fuzzy_groups:
        return _choose_wikidata_pool(fuzzy_groups, confidence=0.65)

    exact_humans = [
        candidate
        for candidate in candidates
        if candidate.normalized_label == target_name and candidate.is_human
    ]
    if len(exact_humans) == 1 and not exact_groups:
        return _choose_wikidata_pool(exact_humans, confidence=0.6)

    return UNKNOWN_GENDER, 0.0, "not_group"


def _choose_musicbrainz_pool(
    pool: list[dict[str, Any]], *, exact: bool
) -> tuple[str, float, str, str | None]:
    if len(pool) > 1:
        genders = {candidate["gender"] for candidate in pool}
        if len(genders) > 1:
            best_score = max(candidate["score"] for candidate in pool)
            return UNKNOWN_GENDER, round(min(0.5, best_score / 200), 2), "not_group", None

    best = sorted(pool, key=lambda item: item["score"], reverse=True)[0]
    if best["gender"] == UNKNOWN_GENDER:
        return UNKNOWN_GENDER, 0.0, "not_group", None
    gender = str(best["gender"])
    group_composition = "unknown" if gender == "group" else "not_group"
    return (
        gender,
        _confidence_from_score(int(best["score"]), exact),
        group_composition,
        str(best["id"]) if best.get("id") else None,
    )


def choose_musicbrainz_group_composition(relations: list[dict[str, Any]]) -> str:
    current_member_genders: list[str] = []
    former_member_genders: list[str] = []

    for relation in relations:
        if not isinstance(relation, dict):
            continue
        relation_type = str(relation.get("type") or "").strip().lower()
        if relation_type not in MUSICBRAINZ_GROUP_MEMBER_RELATION_TYPES:
            continue

        related_artist = relation.get("artist")
        if not isinstance(related_artist, dict):
            continue
        if str(related_artist.get("type") or "").strip().lower() != "person":
            continue

        gender = _musicbrainz_gender_value(related_artist.get("gender"))
        if gender == "group":
            gender = UNKNOWN_GENDER
        if relation.get("ended") is True:
            former_member_genders.append(gender)
        else:
            current_member_genders.append(gender)

    return _group_composition_from_member_genders(current_member_genders or former_member_genders)


def _choose_wikidata_pool(
    pool: list[WikidataCandidate], *, confidence: float
) -> tuple[str, float, str]:
    sorted_pool = sorted(pool, key=lambda candidate: candidate.search_rank)
    if len(sorted_pool) > 1:
        return UNKNOWN_GENDER, 0.0, "not_group"

    best = sorted_pool[0]
    if best.is_group:
        return "group", confidence, best.group_composition
    if best.gender == UNKNOWN_GENDER:
        return UNKNOWN_GENDER, 0.0, "not_group"
    return best.gender, confidence, "not_group"


def _candidate_from_musicbrainz_artist(artist: dict[str, Any]) -> dict[str, Any]:
    artist_type = str(artist.get("type") or "").strip().lower()

    if artist_type == "group":
        gender = "group"
    else:
        gender = _musicbrainz_gender_value(artist.get("gender"))

    return {
        "id": str(artist.get("id") or ""),
        "name": str(artist.get("name") or ""),
        "score": _parse_score(artist.get("score")),
        "gender": gender,
        "type": artist_type,
    }


def _musicbrainz_gender_value(value: Any) -> str:
    mb_gender = str(value or "").strip().lower()
    if mb_gender == "male":
        return "male"
    if mb_gender == "female":
        return "female"
    if mb_gender in {"other", "non-binary", "nonbinary", "transgender", "intersex"}:
        return "other"
    return UNKNOWN_GENDER


def _wikidata_gender(gender_qids: set[str]) -> str:
    if not gender_qids:
        return UNKNOWN_GENDER
    if gender_qids == {MALE_QID}:
        return "male"
    if gender_qids == {FEMALE_QID}:
        return "female"
    return "other"


def _qid_from_wikidata_uri(value: str) -> str | None:
    match = re.search(r"/(Q\d+)$", value)
    return match.group(1) if match else None


def _claim_qids(claims: dict[str, Any], property_id: str) -> set[str]:
    qids: set[str] = set()
    property_claims = claims.get(property_id, [])
    if not isinstance(property_claims, list):
        return qids

    for claim in property_claims:
        if not isinstance(claim, dict):
            continue
        mainsnak = claim.get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {}) if isinstance(mainsnak, dict) else {}
        value = datavalue.get("value", {}) if isinstance(datavalue, dict) else {}
        qid = value.get("id") if isinstance(value, dict) else None
        if isinstance(qid, str) and qid.startswith("Q"):
            qids.add(qid)
    return qids


def _group_member_qids(claims: dict[str, Any]) -> set[str]:
    member_qids: set[str] = set()
    for property_id in GROUP_MEMBER_PROPERTY_IDS:
        member_qids.update(_claim_qids(claims, property_id))
    return member_qids


def _group_composition_from_member_genders(member_genders: list[str]) -> str:
    if not member_genders:
        return UNKNOWN_GENDER

    normalized = [normalize_gender(gender) for gender in member_genders]
    known = [gender for gender in normalized if gender in {"male", "female", "other"}]
    has_unknown = any(gender == UNKNOWN_GENDER for gender in normalized)

    if not known:
        return UNKNOWN_GENDER

    known_types = set(known)
    if len(known_types) > 1:
        return "mixed"
    if has_unknown:
        return UNKNOWN_GENDER

    only = known[0]
    if only == "male":
        return "all_male"
    if only == "female":
        return "all_female"
    return "all_other"


def _group_composition_from_wikidata_hints(instance_of: set[str], description: str) -> str:
    if instance_of & ALL_MALE_GROUP_QIDS:
        return "all_male"
    if instance_of & ALL_FEMALE_GROUP_QIDS:
        return "all_female"

    lower = description.lower()
    if "boy band" in lower:
        return "all_male"
    if "girl group" in lower:
        return "all_female"
    return UNKNOWN_GENDER


def _wikidata_text(entity: dict[str, Any], key: str) -> str:
    values = entity.get(key)
    if not isinstance(values, dict):
        return ""
    english = values.get("en")
    if not isinstance(english, dict):
        return ""
    value = english.get("value")
    return str(value or "")


def _description_is_music_related(description: str) -> bool:
    lower = description.lower()
    return any(term in lower for term in MUSIC_DESCRIPTION_TERMS)


def _description_is_group(description: str) -> bool:
    lower = description.lower()
    return any(term in lower for term in GROUP_DESCRIPTION_TERMS)


def _parse_score(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _confidence_from_score(score: int, exact: bool) -> float:
    score_confidence = max(0.0, min(1.0, score / 100))
    if exact:
        return round(min(0.98, score_confidence), 2)
    return round(min(0.85, score_confidence * 0.85), 2)


def _normalize_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


def _name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_name(left), _normalize_name(right)).ratio()
