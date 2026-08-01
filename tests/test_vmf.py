import asyncio
from unittest.mock import AsyncMock

import pytest

from data.example_config import config as example_config
from src.meta import Meta
from src.trackers.UNIT3D.vmf import VietMediaF


def tracker(*, modq: bool = False) -> VietMediaF:
    return VietMediaF({"DEFAULT": {}, "TRACKERS": {"VMF": {"api_key": "test-key", "modq": modq}}})


def vmf_name(name: str, **meta_values: object) -> str:
    values: dict[str, object] = {"name": name, "resolution": "1080p"}
    values.update(meta_values)
    meta = Meta(**values)
    return asyncio.run(tracker().get_name(meta))["name"]


def mediainfo_audio(**values: str) -> dict[str, object]:
    return {"media": {"track": [{"@type": "General"}, {"@type": "Audio", **values}]}}


def valid_meta(**overrides: object) -> Meta:
    values: dict[str, object] = {
        "category": "MOVIE",
        "type": "WEBDL",
        "resolution": "1080p",
        "tmdb": 123,
        "mediainfo": mediainfo_audio(Language="English"),
    }
    values.update(overrides)
    return Meta(**values)


def test_vmf_profile_uses_canonical_identity_and_unit3d_endpoints():
    vmf = tracker()

    assert vmf.tracker == "VMF"
    assert vmf.display_name == "VietMediaF"
    assert vmf.auth_type == "unit3d_api"
    assert vmf.supported_categories == ("MOVIE", "TV")
    assert vmf.base_url == "https://tracker.vietmediaf.store"
    assert vmf.id_url == "https://tracker.vietmediaf.store/api/torrents/"
    assert vmf.search_url == "https://tracker.vietmediaf.store/api/torrents/filter"
    assert vmf.upload_url == "https://tracker.vietmediaf.store/api/torrents/upload"
    assert vmf.torrent_url == "https://tracker.vietmediaf.store/torrents/"


def test_vmf_is_registered_as_unit3d_api_tracker_with_comment_host():
    from src.trackersetup import api_trackers, get_tracker_comment_hosts, tracker_class_map

    assert tracker_class_map["VMF"] is VietMediaF
    assert "VMF" in api_trackers
    assert get_tracker_comment_hosts({"TRACKERS": {"VMF": {}}})["VMF"] == ("tracker.vietmediaf.store",)


def test_vmf_example_config_has_only_required_tracker_credentials():
    vmf_config = example_config["TRACKERS"]["VMF"]

    assert vmf_config["api_key"] == ""
    assert vmf_config["anon"] is False
    assert vmf_config["modq"] is False
    assert "url" not in vmf_config
    assert "announce_url" not in vmf_config


@pytest.mark.parametrize("language", ["Vietnamese", "vi", "vie", "vi-VN"])
def test_vmf_name_adds_vietnamese_tag_from_audio_language(language: str):
    assert vmf_name("Example Movie 2026 1080p WEB-DL-GRP", audio_languages=[language]) == "Example Movie 2026 ViE 1080p WEB-DL-GRP"


@pytest.mark.parametrize(
    ("field", "title"),
    [
        ("Title", "Lồng Tiếng"),
        ("Title", "long tieng"),
        ("Title_String", "Vietnamese USLT"),
        ("Title_String1", "VNLT"),
    ],
)
def test_vmf_name_classifies_dub_from_mediainfo_audio_titles(field: str, title: str):
    assert vmf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo_audio(**{field: title})) == "Example Movie 2026 ViE DUB 1080p WEB-DL-GRP"


@pytest.mark.parametrize("title", ["Thuyết Minh", "thuyet minh", "Vietnamese TM"])
def test_vmf_name_classifies_voice_over_from_mediainfo_audio_titles(title: str):
    assert vmf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo_audio(Title=title)) == "Example Movie 2026 ViE 1080p WEB-DL-GRP"


def test_vmf_name_ignores_title_fields_on_non_audio_tracks():
    mediainfo = {"media": {"track": [{"@type": "General", "Title": "Lồng Tiếng"}, {"@type": "Video", "Title_String": "VNLT"}]}}

    assert vmf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo) == "Example Movie 2026 1080p WEB-DL-GRP"


def test_vmf_name_preserves_dot_separated_convention_and_legacy_resolution_alias():
    name = "Example.Movie.2026.4K.WEB-DL.DDP5.1.H.265-GRP"

    assert vmf_name(name, resolution="", audio_languages=["Vietnamese"]) == "Example.Movie.2026.ViE.4K.WEB-DL.DDP5.1.H.265-GRP"


def test_vmf_name_falls_back_to_last_source_token_instead_of_title_word():
    name = "Example Web Story 2026 WEB-DL DDP5.1 H.264-GRP"

    assert vmf_name(name, resolution="", source="WEB", audio_languages=["Vietnamese"]) == "Example Web Story 2026 ViE WEB-DL DDP5.1 H.264-GRP"


def test_vmf_name_falls_back_before_release_group():
    name = "Example.Documentary.2025-GRP"

    assert vmf_name(name, resolution="", tag=" -GRP ", audio_languages=["Vietnamese"]) == "Example.Documentary.2025.ViE-GRP"


@pytest.mark.parametrize("title_word", ["Vie", "VIE"])
def test_vmf_name_does_not_treat_title_word_as_existing_tag(title_word: str):
    name = f"Example {title_word} Story 2026 1080p WEB-DL-GRP"

    assert vmf_name(name, audio_languages=["Vietnamese"]) == f"Example {title_word} Story 2026 ViE 1080p WEB-DL-GRP"


@pytest.mark.parametrize(
    ("name", "audio_titles", "languages", "expected"),
    [
        ("Example Movie 2025 ViE 1080p WEB-DL-GRP", {}, ["Vietnamese"], "Example Movie 2025 ViE 1080p WEB-DL-GRP"),
        ("Example Movie 2025 ViE 1080p WEB-DL-GRP", {"Title": "VNLT"}, [], "Example Movie 2025 ViE DUB 1080p WEB-DL-GRP"),
        ("Example Movie 2025 ViE DUB 1080p WEB-DL-GRP", {}, ["Vietnamese"], "Example Movie 2025 ViE DUB 1080p WEB-DL-GRP"),
        ("Example Movie 2025 ViE-DUB 1080p WEB-DL-GRP", {}, ["Vietnamese"], "Example Movie 2025 ViE DUB 1080p WEB-DL-GRP"),
        ("Example Movie 2025 1080p WEB-DL-GRP ViE", {}, ["Vietnamese"], "Example Movie 2025 ViE 1080p WEB-DL-GRP"),
        ("Example.Movie.2025.1080p.WEB-DL-GRP.ViE.DUB", {}, ["Vietnamese"], "Example.Movie.2025.ViE.DUB.1080p.WEB-DL-GRP"),
        ("Example Movie 2025 ViE ViE DUB 1080p WEB-DL-GRP ViE", {}, ["Vietnamese"], "Example Movie 2025 ViE DUB 1080p WEB-DL-GRP"),
        ("Example.Movie.2025.1080p.WEB-DL.ViE-GRP", {}, ["Vietnamese"], "Example.Movie.2025.ViE.1080p.WEB-DL-GRP"),
        ("Example.Movie.2025.ViE-1080p.WEB-DL-GRP", {}, ["Vietnamese"], "Example.Movie.2025.ViE.1080p.WEB-DL-GRP"),
        ("Example Movie 2025 1080p WEB-DL ViE-GRP", {}, ["Vietnamese"], "Example Movie 2025 ViE 1080p WEB-DL-GRP"),
    ],
)
def test_vmf_name_reconciles_existing_tags(name: str, audio_titles: dict[str, str], languages: list[str], expected: str):
    assert vmf_name(name, mediainfo=mediainfo_audio(**audio_titles), audio_languages=languages) == expected


def test_vmf_name_is_idempotent():
    meta = Meta(
        name="Example.Movie.2026.1080p.WEB-DL-GRP",
        resolution="1080p",
        mediainfo=mediainfo_audio(Title_String2="Vietnamese Lồng Tiếng"),
    )
    vmf = tracker()

    first = asyncio.run(vmf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vmf.get_name(meta))["name"]

    assert first == "Example.Movie.2026.ViE.DUB.1080p.WEB-DL-GRP"
    assert second == first


def test_vmf_name_normalizes_whitespace_around_existing_tag_idempotently():
    meta = Meta(name="Example\tViE\t1080p WEB-DL-GRP", resolution="1080p", audio_languages=["Vietnamese"])
    vmf = tracker()

    first = asyncio.run(vmf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vmf.get_name(meta))["name"]

    assert first == "Example ViE 1080p WEB-DL-GRP"
    assert second == first


def test_vmf_name_leaves_non_vietnamese_release_unchanged():
    name = "Example.Movie.2025.1080p.WEB-DL-GRP"

    assert vmf_name(name, audio_languages=["English"], mediainfo=mediainfo_audio(Title="English")) == name


@pytest.mark.parametrize("tmdb", [None, 0, "0", "invalid"])
def test_vmf_checks_require_valid_tmdb(tmdb: object):
    assert asyncio.run(tracker().get_additional_checks(valid_meta(tmdb=tmdb))) is False


def test_vmf_checks_accept_tmdb_id_fallback():
    meta = valid_meta(tmdb="invalid", tmdb_id=123)

    assert asyncio.run(tracker().get_additional_checks(meta)) is True
    assert asyncio.run(tracker().get_tmdb(meta)) == {"tmdb": "123"}


def test_vmf_dupe_search_uses_same_tmdb_fallback_as_upload_payload():
    meta = valid_meta(tmdb="invalid", tmdb_id=123, tracker_status={"VMF": {}})
    vmf = tracker()
    vmf.get_search_urls = AsyncMock(return_value=[])

    assert asyncio.run(vmf.search_existing(meta)) == []
    request_params = vmf.get_search_urls.await_args.args[1]
    assert dict(request_params)["tmdbId"] == "123"


@pytest.mark.parametrize(
    ("field", "value"),
    [("category", "MUSIC"), ("type", "UNKNOWN"), ("resolution", "360p")],
)
def test_vmf_checks_reject_unmapped_unit3d_taxonomy(field: str, value: str):
    assert asyncio.run(tracker().get_additional_checks(valid_meta(**{field: value}))) is False


def test_vmf_checks_require_mediainfo_or_bdinfo():
    assert asyncio.run(tracker().get_additional_checks(valid_meta(mediainfo={}, bdinfo={}))) is False
    assert asyncio.run(tracker().get_additional_checks(valid_meta(mediainfo={}, bdinfo={"title": "Example"}))) is True


def test_vmf_checks_accept_valid_tv_episode_and_season_pack():
    episode = valid_meta(category="TV", season_int=1, episode_int=2, tv_pack=False)
    season_pack = valid_meta(category="TV", season_int=1, episode_int=0, tv_pack=True)

    assert asyncio.run(tracker().get_additional_checks(episode)) is True
    assert asyncio.run(tracker().get_additional_checks(season_pack)) is True


@pytest.mark.parametrize(
    ("season_int", "episode_int", "tv_pack"),
    [(0, 1, False), (1, 0, False), (0, 0, True)],
)
def test_vmf_checks_reject_incomplete_tv_metadata(season_int: int, episode_int: int, tv_pack: bool):
    meta = valid_meta(category="TV", season_int=season_int, episode_int=episode_int, tv_pack=tv_pack)

    assert asyncio.run(tracker().get_additional_checks(meta)) is False


def test_vmf_checks_allow_mediainfo_without_encode_settings():
    meta = valid_meta(valid_mi_settings=False, has_encode_settings=False)

    assert asyncio.run(tracker().get_additional_checks(meta)) is True


def test_vmf_mod_queue_payload_honors_config_and_meta_override():
    assert asyncio.run(tracker(modq=True).get_additional_data(Meta())) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta(modq=True))) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta())) == {"mod_queue_opt_in": "0"}
