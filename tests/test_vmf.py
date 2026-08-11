import asyncio
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from data.example_config import config as example_config
from src.audio import bloated_check
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
    assert vmf.requests_url == "https://tracker.vietmediaf.store/api/requests/filter"
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


@pytest.mark.parametrize("language", ["vi", "vie", "vi-VN", "Vietnamese", "Tiếng Việt", "tieng viet"])
def test_vmf_allows_vietnamese_audio_without_bloat_warning(monkeypatch: pytest.MonkeyPatch, language: str):
    trackersetup_stub = ModuleType("src.trackersetup")
    trackersetup_stub.tracker_class_map = {"VMF": VietMediaF}
    monkeypatch.setitem(sys.modules, "src.trackersetup", trackersetup_stub)
    meta = Meta(trackers=["VMF"])

    bloated_check(meta, [language])

    assert meta.bloated is False


def test_vmf_still_warns_for_unrelated_bloated_audio(monkeypatch: pytest.MonkeyPatch):
    trackersetup_stub = ModuleType("src.trackersetup")
    trackersetup_stub.tracker_class_map = {"VMF": VietMediaF}
    monkeypatch.setitem(sys.modules, "src.trackersetup", trackersetup_stub)
    meta = Meta(trackers=["VMF"])

    bloated_check(meta, ["fr"])

    assert meta.bloated is True


@pytest.mark.parametrize("language", ["Vietnamese", "vi", "vie", "vi-VN", "Tiếng Việt", "tieng viet"])
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
        ("Example Movie 2025 [ViE] 1080p WEB-DL-GRP", {}, [], "Example Movie 2025 ViE 1080p WEB-DL-GRP"),
        ("Example Movie 2025 ([ViE]) 1080p WEB-DL-GRP", {}, [], "Example Movie 2025 ViE 1080p WEB-DL-GRP"),
        ("Example Movie 2025 VIE DUB 1080p WEB-DL-GRP", {}, [], "Example Movie 2025 ViE DUB 1080p WEB-DL-GRP"),
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


def test_vmf_name_does_not_treat_other_resolution_as_title_token():
    assert (
        vmf_name(
            "The Other Side 2020 1080p BluRay-GRP",
            resolution="OTHER",
            source="BluRay",
            audio_languages=["Vietnamese"],
        )
        == "The Other Side 2020 ViE 1080p BluRay-GRP"
    )


def test_vmf_name_does_not_treat_web_title_word_as_source():
    assert (
        vmf_name(
            "Charlotte's Web-GRP",
            resolution="",
            source="WEB",
            tag="-GRP",
            audio_languages=["Vietnamese"],
        )
        == "Charlotte's Web ViE-GRP"
    )


def test_vmf_name_does_not_treat_4k_title_word_as_resolution():
    assert (
        vmf_name(
            "Project 4K WEB-DL-GRP",
            resolution="OTHER",
            source="WEB",
            audio_languages=["Vietnamese"],
        )
        == "Project 4K ViE WEB-DL-GRP"
    )


@pytest.mark.parametrize("resolution", ["", "OTHER", "UNKNOWN"])
def test_vmf_name_uses_contextual_4k_alias_with_unknown_resolution(resolution: str):
    assert (
        vmf_name(
            "Example Movie 2024 4K HDR WEB-DL-GRP",
            resolution=resolution,
            source="WEB",
            audio_languages=["Vietnamese"],
        )
        == "Example Movie 2024 ViE 4K HDR WEB-DL-GRP"
    )


def test_vmf_name_does_not_use_trailing_group_year_as_anchor_boundary():
    assert (
        vmf_name(
            "Movie.2020.1080p.BluRay.BT.2024-GRP",
            resolution="1080p",
            source="BluRay",
            audio_languages=["Vietnamese"],
        )
        == "Movie.2020.ViE.1080p.BluRay.BT.2024-GRP"
    )


def test_vmf_name_is_idempotent_for_hyphen_separated_input():
    meta = Meta(
        name="Example-Movie-2020-1080p-WEB-DL-GRP",
        resolution="1080p",
        audio_languages=["Vietnamese"],
    )
    vmf = tracker()

    first = asyncio.run(vmf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vmf.get_name(meta))["name"]

    assert first == "Example-Movie-2020-ViE-1080p-WEB-DL-GRP"
    assert second == first


def test_vmf_name_leaves_non_vietnamese_release_unchanged():
    name = "Example.Movie.2025.1080p.WEB-DL-GRP"

    assert vmf_name(name, audio_languages=["English"], mediainfo=mediainfo_audio(Title="English")) == name


def test_vmf_uses_tmdb_id_fallback():
    meta = valid_meta(tmdb="invalid", tmdb_id=123)

    assert asyncio.run(tracker().get_tmdb(meta)) == {"tmdb": "123"}


def test_vmf_dupe_search_uses_same_tmdb_fallback_as_upload_payload():
    meta = valid_meta(tmdb="invalid", tmdb_id=123, tracker_status={"VMF": {}})
    vmf = tracker()
    vmf.get_search_urls = AsyncMock(return_value=[])

    assert asyncio.run(vmf.search_existing(meta)) == []
    request_params = vmf.get_search_urls.await_args.args[1]
    assert dict(request_params)["tmdbId"] == "123"


def test_vmf_request_search_uses_endpoint_and_other_resolution_mapping(tmp_path):
    from src.trackersetup import TrackerSetup

    config = {"DEFAULT": {}, "TRACKERS": {"VMF": {"api_key": "test-key", "modq": False}}}
    setup = TrackerSetup(config)
    setup.get_tracker_requests = AsyncMock(
        return_value=[
            {
                "id": 42,
                "name": "Example request",
                "description": "",
                "category": "1",
                "type": "4",
                "resolution": "10",
                "bounty": 100,
                "status": "unfilled",
                "claimed": False,
                "season": None,
                "episode": None,
            }
        ]
    )
    meta = valid_meta(resolution="OTHER", base_dir=str(tmp_path), uuid="vmf-request-test", path="Example.mkv")

    assert asyncio.run(setup.tracker_request(meta, "VMF")) is True
    assert setup.get_tracker_requests.await_args.args[2] == "https://tracker.vietmediaf.store/api/requests/filter"
    request_log = json.loads((tmp_path / "tmp" / "VMF_request_results.json").read_text(encoding="utf-8"))
    assert request_log[0]["url"] == "https://tracker.vietmediaf.store/requests/42"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tmdb": None, "tmdb_id": None},
        {"category": "MUSIC"},
        {"type": "UNKNOWN"},
        {"resolution": "OTHER"},
        {"mediainfo": {}, "bdinfo": {}},
        {"category": "TV", "season_int": 0, "episode_int": 0, "tv_pack": False},
    ],
)
def test_vmf_does_not_add_stricter_pre_upload_checks(overrides: dict[str, object]):
    assert asyncio.run(tracker().get_additional_checks(valid_meta(**overrides))) is True


def test_vmf_uses_unit3d_other_resolution_fallback():
    meta = valid_meta(resolution="OTHER")

    assert asyncio.run(tracker().get_resolution_id(meta)) == {"resolution_id": "10"}
    assert asyncio.run(tracker().get_resolution_id(meta, mapping_only=True))["OTHER"] == "10"
    assert asyncio.run(tracker().get_resolution_id(meta, reverse=True))["10"] == "8640p"


def test_vmf_mod_queue_payload_honors_config_and_meta_override():
    assert asyncio.run(tracker(modq=True).get_additional_data(Meta())) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta(modq=True))) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta())) == {"mod_queue_opt_in": "0"}
