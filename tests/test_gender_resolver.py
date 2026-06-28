from __future__ import annotations

import httpx

from cache import ArtistGenderCache
from gender_resolver import (
    FEMALE_QID,
    HUMAN_QID,
    MALE_QID,
    MUSIC_GROUP_QIDS,
    MUSIC_OCCUPATION_QIDS,
    GenderResolver,
    WikidataCandidate,
    choose_musicbrainz_group_composition,
    choose_musicbrainz_classification,
    choose_musicbrainz_gender,
    choose_wikidata_artist_role,
    choose_wikidata_classification,
    choose_wikidata_gender,
)


def test_choose_musicbrainz_gender_person_male() -> None:
    gender, confidence = choose_musicbrainz_gender(
        "Artist B",
        [{"name": "Artist B", "score": 100, "type": "Person", "gender": "male"}],
    )

    assert gender == "male"
    assert confidence == 0.98


def test_choose_musicbrainz_gender_group() -> None:
    gender, confidence = choose_musicbrainz_gender(
        "Band C",
        [{"name": "Band C", "score": 100, "type": "Group"}],
    )

    assert gender == "group"
    assert confidence == 0.98


def test_choose_musicbrainz_gender_other_for_non_binary() -> None:
    gender, confidence = choose_musicbrainz_gender(
        "Artist C",
        [{"name": "Artist C", "score": 100, "type": "Person", "gender": "non-binary"}],
    )

    assert gender == "other"
    assert confidence == 0.98


def test_choose_musicbrainz_gender_not_applicable_is_unknown_for_person() -> None:
    gender, confidence = choose_musicbrainz_gender(
        "Artist C",
        [{"name": "Artist C", "score": 100, "type": "Person", "gender": "not applicable"}],
    )

    assert gender == "unknown"
    assert confidence == 0.0


def test_choose_musicbrainz_gender_ambiguous_results_are_unknown() -> None:
    gender, confidence = choose_musicbrainz_gender(
        "Alex Example",
        [
            {"name": "Alex Example", "score": 99, "type": "Person", "gender": "male"},
            {"name": "Alex Example", "score": 98, "type": "Person", "gender": "female"},
        ],
    )

    assert gender == "unknown"
    assert confidence <= 0.5


def test_resolver_uses_cache_before_musicbrainz(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")
    cache.set("spotify-id", "Cached Artist", "male", "manual", 1.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("MusicBrainz should not be called for cached artists")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Cached Artist")

    assert result.gender == "male"
    assert result.source == "manual"


def test_resolver_caches_musicbrainz_result(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "artists": [
                    {
                        "name": "Artist B",
                        "score": 100,
                        "type": "Person",
                        "gender": "male",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Artist B")

    assert result.gender == "male"
    assert cache.get("spotify-id")["gender"] == "male"


def test_resolver_marks_male_composer_role_from_wikidata(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-composer",
                            "name": "Composer A",
                            "score": 100,
                            "type": "Person",
                            "gender": "male",
                        }
                    ]
                },
            )
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q300",
                            "label": "Composer A",
                            "description": "film score composer",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q300": {
                            "labels": {"en": {"value": "Composer A"}},
                            "descriptions": {"en": {"value": "film score composer"}},
                            "claims": {
                                "P31": [_claim(HUMAN_QID)],
                                "P106": [_claim("Q36834")],
                                "P21": [_claim(MALE_QID)],
                            },
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Composer A")

    assert result.gender == "male"
    assert result.artist_role == "composer_or_score"
    assert result.source == "musicbrainz+wikidata"
    assert cache.get("spotify-id")["artist_role"] == "composer_or_score"


def test_resolver_keeps_generic_composer_without_performer_role(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-classical-composer",
                            "name": "Classical Composer",
                            "score": 100,
                            "type": "Person",
                            "gender": "male",
                        }
                    ]
                },
            )
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q301",
                            "label": "Classical Composer",
                            "description": "Japanese composer and conductor",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q301": {
                            "labels": {"en": {"value": "Classical Composer"}},
                            "descriptions": {
                                "en": {"value": "Japanese composer and conductor"}
                            },
                            "claims": {
                                "P31": [_claim(HUMAN_QID)],
                                "P106": [_claim("Q36834")],
                                "P21": [_claim(MALE_QID)],
                            },
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Classical Composer")

    assert result.gender == "male"
    assert result.artist_role == "composer_or_score"
    assert result.source == "musicbrainz+wikidata"


def test_resolver_does_not_keep_singer_songwriter_as_composer(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-singer-songwriter",
                            "name": "Singer Songwriter",
                            "score": 100,
                            "type": "Person",
                            "gender": "male",
                        }
                    ]
                },
            )
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q302",
                            "label": "Singer Songwriter",
                            "description": "American singer-songwriter and composer",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q302": {
                            "labels": {"en": {"value": "Singer Songwriter"}},
                            "descriptions": {
                                "en": {"value": "American singer-songwriter and composer"}
                            },
                            "claims": {
                                "P31": [_claim(HUMAN_QID)],
                                "P106": [_claim("Q36834"), _claim("Q488205")],
                                "P21": [_claim(MALE_QID)],
                            },
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Singer Songwriter")

    assert result.gender == "male"
    assert result.artist_role == "unknown"
    assert result.source == "musicbrainz"


def test_musicbrainz_tags_can_mark_composer_role() -> None:
    gender, confidence, group_composition, _artist_id, artist_role = (
        choose_musicbrainz_classification(
            "Composer B",
            [
                {
                    "id": "mbid-composer-b",
                    "name": "Composer B",
                    "score": 100,
                    "type": "Person",
                    "gender": "male",
                    "tags": [{"name": "film score"}],
                }
            ],
        )
    )

    assert gender == "male"
    assert confidence == 0.98
    assert group_composition == "not_group"
    assert artist_role == "composer_or_score"


def test_resolver_enriches_musicbrainz_group_with_artist_rels(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "musicbrainz.org" in str(request.url) and request.url.path.endswith("/artist"):
            return httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-band-d",
                            "name": "Band D",
                            "score": 100,
                            "type": "Group",
                        }
                    ]
                },
            )
        if "musicbrainz.org" in str(request.url) and request.url.path.endswith(
            "/artist/mbid-band-d"
        ):
            return httpx.Response(
                200,
                json={
                    "relations": [
                        {
                            "type": "member of band",
                            "artist": {"type": "Person", "gender": "male"},
                            "ended": False,
                        },
                        {
                            "type": "member of band",
                            "artist": {"type": "Person", "gender": "male"},
                            "ended": False,
                        },
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Band D")

    assert result.gender == "group"
    assert result.group_composition == "all_male"
    assert result.source == "musicbrainz"
    assert cache.get("spotify-id")["group_composition"] == "all_male"


def test_musicbrainz_group_composition_uses_current_artist_rels() -> None:
    group_composition = choose_musicbrainz_group_composition(
        [
            {
                "type": "member of band",
                "artist": {"type": "Person", "gender": "male"},
                "ended": False,
            },
            {
                "type": "member of band",
                "artist": {"type": "Person", "gender": "female"},
                "ended": False,
            },
            {
                "type": "member of band",
                "artist": {"type": "Person", "gender": "male"},
                "ended": True,
            },
        ]
    )

    assert group_composition == "mixed"


def test_resolver_falls_back_to_wikidata_when_musicbrainz_is_unknown(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(200, json={"artists": []})
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q1",
                            "label": "Artist D",
                            "description": "American singer",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q1": {
                            "labels": {"en": {"value": "Artist D"}},
                            "descriptions": {"en": {"value": "American singer"}},
                            "claims": {
                                "P31": [_claim(HUMAN_QID)],
                                "P106": [_claim(next(iter(MUSIC_OCCUPATION_QIDS)))],
                                "P21": [_claim(FEMALE_QID)],
                            },
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Artist D")

    assert result.gender == "female"
    assert result.source == "wikidata"
    assert cache.get("spotify-id")["gender"] == "female"


def test_wikidata_group_composition_uses_p463_membership(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(200, json={"artists": []})
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q100",
                            "label": "Band F",
                            "description": "musical group",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q100": {
                            "labels": {"en": {"value": "Band F"}},
                            "descriptions": {"en": {"value": "musical group"}},
                            "claims": {"P31": [_claim(next(iter(MUSIC_GROUP_QIDS)))]},
                        }
                    }
                },
            )
        if "query.wikidata.org" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": {
                        "bindings": [
                            {
                                "group": {
                                    "value": "http://www.wikidata.org/entity/Q100"
                                },
                                "member": {
                                    "value": "http://www.wikidata.org/entity/QMember1"
                                },
                                "gender": {
                                    "value": f"http://www.wikidata.org/entity/{MALE_QID}"
                                },
                            },
                            {
                                "group": {
                                    "value": "http://www.wikidata.org/entity/Q100"
                                },
                                "member": {
                                    "value": "http://www.wikidata.org/entity/QMember2"
                                },
                                "gender": {
                                    "value": f"http://www.wikidata.org/entity/{FEMALE_QID}"
                                },
                            },
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Band F")

    assert result.gender == "group"
    assert result.group_composition == "mixed"
    assert result.source == "wikidata"


def test_wikidata_boy_band_description_implies_all_male_group(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(200, json={"artists": []})
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q200",
                            "label": "CORTIS",
                            "description": "South Korean boy band",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q200": {
                            "labels": {"en": {"value": "CORTIS"}},
                            "descriptions": {"en": {"value": "South Korean boy band"}},
                            "claims": {},
                        }
                    }
                },
            )
        if "query.wikidata.org" in str(request.url):
            return httpx.Response(200, json={"results": {"bindings": []}})
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "CORTIS")

    assert result.gender == "group"
    assert result.group_composition == "all_male"
    assert result.source == "wikidata"


def test_resolver_falls_back_to_wikidata_when_musicbrainz_errors(tmp_path) -> None:
    cache = ArtistGenderCache(tmp_path / "cache.json")

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if "musicbrainz.org" in str(request.url):
            return httpx.Response(429, json={"error": "rate limited"})
        if action == "wbsearchentities":
            return httpx.Response(
                200,
                json={
                    "search": [
                        {
                            "id": "Q2",
                            "label": "Artist E",
                            "description": "Canadian musician",
                        }
                    ]
                },
            )
        if action == "wbgetentities":
            return httpx.Response(
                200,
                json={
                    "entities": {
                        "Q2": {
                            "labels": {"en": {"value": "Artist E"}},
                            "descriptions": {"en": {"value": "Canadian musician"}},
                            "claims": {
                                "P31": [_claim(HUMAN_QID)],
                                "P106": [_claim(next(iter(MUSIC_OCCUPATION_QIDS)))],
                                "P21": [_claim(MALE_QID)],
                            },
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GenderResolver(cache, client=client, min_request_interval_seconds=0)

    result = resolver.resolve_artist("spotify-id", "Artist E")

    assert result.gender == "male"
    assert result.source == "wikidata"


def test_choose_wikidata_gender_maps_non_binary_to_other() -> None:
    gender, confidence = choose_wikidata_gender(
        "Artist F",
        [
            WikidataCandidate(
                entity_id="Q3",
                label="Artist F",
                description="singer",
                search_rank=0,
                gender="other",
                is_human=True,
                is_group=False,
                is_music_related=True,
            )
        ],
    )

    assert gender == "other"
    assert confidence > 0


def test_choose_wikidata_classification_returns_group_composition() -> None:
    gender, confidence, group_composition = choose_wikidata_classification(
        "Band E",
        [
            WikidataCandidate(
                entity_id="Q6",
                label="Band E",
                description="musical group",
                search_rank=0,
                gender="unknown",
                is_human=False,
                is_group=True,
                is_music_related=False,
                group_composition="mixed",
            )
        ],
        prefer_group=True,
    )

    assert gender == "group"
    assert group_composition == "mixed"
    assert confidence > 0


def test_choose_wikidata_gender_multiple_strong_matches_are_unknown() -> None:
    gender, confidence = choose_wikidata_gender(
        "Artist G",
        [
            WikidataCandidate("Q4", "Artist G", "singer", 0, "male", True, False, True),
            WikidataCandidate("Q5", "Artist G", "singer", 1, "female", True, False, True),
        ],
    )

    assert gender == "unknown"
    assert confidence == 0.0


def _claim(qid: str) -> dict:
    return {"mainsnak": {"datavalue": {"value": {"id": qid}}}}
