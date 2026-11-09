import asyncio
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from data.example_config import config as example_config
from src.audio import bloated_check
from src.meta import Meta
from src.trackers.UNIT3D.vietmediaf import VietMediaF


def tracker(*, modq: bool = False) -> VietMediaF:
    return VietMediaF({"DEFAULT": {}, "TRACKERS": {"VIETMEDIAF": {"api_key": "test-key", "modq": modq}}})


def vietmediaf_name(name: str, **meta_values: object) -> str:
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


def test_vietmediaf_profile_uses_canonical_identity_and_unit3d_endpoints():
    vietmediaf = tracker()

    assert vietmediaf.tracker == "VIETMEDIAF"
    assert vietmediaf.display_name == "VietMediaF"
    assert vietmediaf.auth_type == "unit3d_api"
    assert vietmediaf.supported_categories == ("MOVIE", "TV")
    assert vietmediaf.base_url == "https://tracker.vietmediaf.store"
    assert vietmediaf.id_url == "https://tracker.vietmediaf.store/api/torrents/"
    assert vietmediaf.requests_url == "https://tracker.vietmediaf.store/api/requests/filter"
    assert vietmediaf.search_url == "https://tracker.vietmediaf.store/api/torrents/filter"
    assert vietmediaf.upload_url == "https://tracker.vietmediaf.store/api/torrents/upload"
    assert vietmediaf.torrent_url == "https://tracker.vietmediaf.store/torrents/"
    assert vietmediaf.search_max_retries == -1
    assert vietmediaf.banned_groups == ()
    assert vietmediaf.bad_release_groups == ("YIFY", "YTS", "mSD", "MeGusta", "3cTWeB", "FGT")


def test_vietmediaf_is_registered_as_unit3d_api_tracker_with_comment_host():
    from src.trackersetup import api_trackers, get_tracker_comment_hosts, tracker_class_map

    assert tracker_class_map["VIETMEDIAF"] is VietMediaF
    assert "VIETMEDIAF" in api_trackers
    assert get_tracker_comment_hosts({"TRACKERS": {"VIETMEDIAF": {}}})["VIETMEDIAF"] == ("tracker.vietmediaf.store",)


def test_vietmediaf_example_config_has_only_required_tracker_credentials():
    vietmediaf_config = example_config["TRACKERS"]["VIETMEDIAF"]

    assert vietmediaf_config["api_key"] == ""
    assert vietmediaf_config["anon"] is False
    assert vietmediaf_config["modq"] is False
    assert "url" not in vietmediaf_config
    assert "announce_url" not in vietmediaf_config


@pytest.mark.parametrize("language", ["vi", "vie", "vi-VN", "Vietnamese", "Tiếng Việt", "tieng viet"])
def test_vietmediaf_allows_vietnamese_audio_without_bloat_warning(monkeypatch: pytest.MonkeyPatch, language: str):
    trackersetup_stub = ModuleType("src.trackersetup")
    trackersetup_stub.tracker_class_map = {"VIETMEDIAF": VietMediaF}
    monkeypatch.setitem(sys.modules, "src.trackersetup", trackersetup_stub)
    meta = Meta(trackers=["VIETMEDIAF"])

    bloated_check(meta, [language])

    assert meta.bloated is False


def test_vietmediaf_still_warns_for_unrelated_bloated_audio(monkeypatch: pytest.MonkeyPatch):
    trackersetup_stub = ModuleType("src.trackersetup")
    trackersetup_stub.tracker_class_map = {"VIETMEDIAF": VietMediaF}
    monkeypatch.setitem(sys.modules, "src.trackersetup", trackersetup_stub)
    meta = Meta(trackers=["VIETMEDIAF"])

    bloated_check(meta, ["fr"])

    assert meta.bloated is True


@pytest.mark.parametrize("language", ["Vietnamese", "vi", "vie", "vi-VN", "Tiếng Việt", "tieng viet"])
def test_vietmediaf_name_adds_vietnamese_tag_from_audio_language(language: str):
    assert vietmediaf_name("Example Movie 2026 1080p WEB-DL-GRP", audio_languages=[language]) == "Example Movie 2026 ViE 1080p WEB-DL-GRP"


@pytest.mark.parametrize(
    ("field", "title"),
    [
        ("Title", "Lồng Tiếng"),
        ("Title", "long tieng"),
        ("Title_String", "Vietnamese USLT"),
        ("Title_String1", "VNLT"),
    ],
)
def test_vietmediaf_name_classifies_dub_from_mediainfo_audio_titles(field: str, title: str):
    assert vietmediaf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo_audio(**{field: title})) == "Example Movie 2026 ViE DUB 1080p WEB-DL-GRP"


@pytest.mark.parametrize("title", ["Thuyết Minh", "thuyet minh", "Vietnamese TM"])
def test_vietmediaf_name_classifies_voice_over_from_mediainfo_audio_titles(title: str):
    assert vietmediaf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo_audio(Title=title)) == "Example Movie 2026 ViE 1080p WEB-DL-GRP"


def test_vietmediaf_name_ignores_title_fields_on_non_audio_tracks():
    mediainfo = {"media": {"track": [{"@type": "General", "Title": "Lồng Tiếng"}, {"@type": "Video", "Title_String": "VNLT"}]}}

    assert vietmediaf_name("Example Movie 2026 1080p WEB-DL-GRP", mediainfo=mediainfo) == "Example Movie 2026 1080p WEB-DL-GRP"


def test_vietmediaf_name_preserves_dot_separated_convention_and_legacy_resolution_alias():
    name = "Example.Movie.2026.4K.WEB-DL.DDP5.1.H.265-GRP"

    assert vietmediaf_name(name, resolution="", audio_languages=["Vietnamese"]) == "Example.Movie.2026.ViE.4K.WEB-DL.DDP5.1.H.265-GRP"


def test_vietmediaf_name_falls_back_to_last_source_token_instead_of_title_word():
    name = "Example Web Story 2026 WEB-DL DDP5.1 H.264-GRP"

    assert vietmediaf_name(name, resolution="", source="WEB", audio_languages=["Vietnamese"]) == "Example Web Story 2026 ViE WEB-DL DDP5.1 H.264-GRP"


def test_vietmediaf_name_falls_back_before_release_group():
    name = "Example.Documentary.2025-GRP"

    assert vietmediaf_name(name, resolution="", tag=" -GRP ", audio_languages=["Vietnamese"]) == "Example.Documentary.2025.ViE-GRP"


@pytest.mark.parametrize("title_word", ["Vie", "VIE"])
def test_vietmediaf_name_does_not_treat_title_word_as_existing_tag(title_word: str):
    name = f"Example {title_word} Story 2026 1080p WEB-DL-GRP"

    assert vietmediaf_name(name, audio_languages=["Vietnamese"]) == f"Example {title_word} Story 2026 ViE 1080p WEB-DL-GRP"


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
def test_vietmediaf_name_reconciles_existing_tags(name: str, audio_titles: dict[str, str], languages: list[str], expected: str):
    assert vietmediaf_name(name, mediainfo=mediainfo_audio(**audio_titles), audio_languages=languages) == expected


def test_vietmediaf_name_is_idempotent():
    meta = Meta(
        name="Example.Movie.2026.1080p.WEB-DL-GRP",
        resolution="1080p",
        mediainfo=mediainfo_audio(Title_String2="Vietnamese Lồng Tiếng"),
    )
    vietmediaf = tracker()

    first = asyncio.run(vietmediaf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vietmediaf.get_name(meta))["name"]

    assert first == "Example.Movie.2026.ViE.DUB.1080p.WEB-DL-GRP"
    assert second == first


def test_vietmediaf_name_normalizes_whitespace_around_existing_tag_idempotently():
    meta = Meta(name="Example\tViE\t1080p WEB-DL-GRP", resolution="1080p", audio_languages=["Vietnamese"])
    vietmediaf = tracker()

    first = asyncio.run(vietmediaf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vietmediaf.get_name(meta))["name"]

    assert first == "Example ViE 1080p WEB-DL-GRP"
    assert second == first


def test_vietmediaf_name_does_not_treat_other_resolution_as_title_token():
    assert (
        vietmediaf_name(
            "The Other Side 2020 1080p BluRay-GRP",
            resolution="OTHER",
            source="BluRay",
            audio_languages=["Vietnamese"],
        )
        == "The Other Side 2020 ViE 1080p BluRay-GRP"
    )


def test_vietmediaf_name_does_not_treat_web_title_word_as_source():
    assert (
        vietmediaf_name(
            "Charlotte's Web-GRP",
            resolution="",
            source="WEB",
            tag="-GRP",
            audio_languages=["Vietnamese"],
        )
        == "Charlotte's Web ViE-GRP"
    )


def test_vietmediaf_name_does_not_treat_4k_title_word_as_resolution():
    assert (
        vietmediaf_name(
            "Project 4K WEB-DL-GRP",
            resolution="OTHER",
            source="WEB",
            audio_languages=["Vietnamese"],
        )
        == "Project 4K ViE WEB-DL-GRP"
    )


@pytest.mark.parametrize("resolution", ["", "OTHER", "UNKNOWN"])
def test_vietmediaf_name_uses_contextual_4k_alias_with_unknown_resolution(resolution: str):
    assert (
        vietmediaf_name(
            "Example Movie 2024 4K HDR WEB-DL-GRP",
            resolution=resolution,
            source="WEB",
            audio_languages=["Vietnamese"],
        )
        == "Example Movie 2024 ViE 4K HDR WEB-DL-GRP"
    )


def test_vietmediaf_name_does_not_use_trailing_group_year_as_anchor_boundary():
    assert (
        vietmediaf_name(
            "Movie.2020.1080p.BluRay.BT.2024-GRP",
            resolution="1080p",
            source="BluRay",
            audio_languages=["Vietnamese"],
        )
        == "Movie.2020.ViE.1080p.BluRay.BT.2024-GRP"
    )


def test_vietmediaf_name_is_idempotent_for_hyphen_separated_input():
    meta = Meta(
        name="Example-Movie-2020-1080p-WEB-DL-GRP",
        resolution="1080p",
        audio_languages=["Vietnamese"],
    )
    vietmediaf = tracker()

    first = asyncio.run(vietmediaf.get_name(meta))["name"]
    meta.name = first
    second = asyncio.run(vietmediaf.get_name(meta))["name"]

    assert first == "Example-Movie-2020-ViE-1080p-WEB-DL-GRP"
    assert second == first


def test_vietmediaf_name_leaves_non_vietnamese_release_unchanged():
    name = "Example.Movie.2025.1080p.WEB-DL-GRP"

    assert vietmediaf_name(name, audio_languages=["English"], mediainfo=mediainfo_audio(Title="English")) == name


def test_vietmediaf_uses_tmdb_id_fallback():
    meta = valid_meta(tmdb="invalid", tmdb_id=123)

    assert asyncio.run(tracker().get_tmdb(meta)) == {"tmdb": "123"}


def test_vietmediaf_dupe_search_uses_same_tmdb_fallback_as_upload_payload():
    meta = valid_meta(tmdb="invalid", tmdb_id=123, tracker_status={"VIETMEDIAF": {}})
    vietmediaf = tracker()
    vietmediaf.get_search_urls = AsyncMock(return_value=[])

    assert asyncio.run(vietmediaf.search_existing(meta)) == []
    request_params = vietmediaf.get_search_urls.await_args.args[1]
    assert dict(request_params)["tmdbId"] == "123"


def test_vietmediaf_request_search_uses_endpoint_and_other_resolution_mapping(tmp_path):
    from src.trackersetup import TrackerSetup

    config = {"DEFAULT": {}, "TRACKERS": {"VIETMEDIAF": {"api_key": "test-key", "modq": False}}}
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
    meta = valid_meta(resolution="OTHER", base_dir=str(tmp_path), uuid="vietmediaf-request-test", path="Example.mkv")

    assert asyncio.run(setup.tracker_request(meta, "VIETMEDIAF")) is True
    assert setup.get_tracker_requests.await_args.args[2] == "https://tracker.vietmediaf.store/api/requests/filter"
    request_log = json.loads((tmp_path / "tmp" / "VIETMEDIAF_request_results.json").read_text(encoding="utf-8"))
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
def test_vietmediaf_does_not_add_stricter_pre_upload_checks(overrides: dict[str, object]):
    assert asyncio.run(tracker().get_additional_checks(valid_meta(**overrides))) is True


def test_vietmediaf_uses_unit3d_other_resolution_fallback():
    meta = valid_meta(resolution="OTHER")

    assert asyncio.run(tracker().get_resolution_id(meta)) == {"resolution_id": "10"}
    assert asyncio.run(tracker().get_resolution_id(meta, mapping_only=True))["OTHER"] == "10"
    assert asyncio.run(tracker().get_resolution_id(meta, reverse=True))["10"] == "8640p"


def test_vietmediaf_mod_queue_payload_honors_config_and_meta_override():
    assert asyncio.run(tracker(modq=True).get_additional_data(Meta())) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta(modq=True))) == {"mod_queue_opt_in": "1"}
    assert asyncio.run(tracker(modq=False).get_additional_data(Meta())) == {"mod_queue_opt_in": "0"}


def test_vietmediaf_slot_rules_separate_original_vie_and_dub_slots():
    from src.slot_rules.models import Verdict
    from src.slot_rules.profile import H264, WEBDL, ReleaseProfile
    from src.slot_rules.trackers.vietmediaf import VietMediaFSlotRules

    upload_vie = ReleaseProfile(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
    )
    existing_orig = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
    )
    existing_vie = ReleaseProfile(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-OTHER",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
    )
    rules = VietMediaFSlotRules()

    # Upload ViE when only Original is on site -> Allowed
    decision = rules.evaluate(upload_vie, [existing_orig])
    assert decision is not None
    assert decision.verdict == Verdict.ALLOW
    assert "ViE" in decision.slot

    # Upload ViE when another ViE is on site -> Blocked
    decision_blocked = rules.evaluate(upload_vie, [existing_vie])
    assert decision_blocked is not None
    assert decision_blocked.verdict == Verdict.BLOCK

    # Upload ViE DUB when ViE and Original are on site -> Allowed
    upload_dub = ReleaseProfile(
        name="Movie.2024.ViE.DUB.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
    )
    decision_dub = rules.evaluate(upload_dub, [existing_orig, existing_vie])
    assert decision_dub is not None
    assert decision_dub.verdict == Verdict.ALLOW
    assert "ViE DUB" in decision_dub.slot


def test_vietmediaf_dupe_checker_excludes_mismatched_audio_tags():
    from src.dupe_checking import DupeChecker

    checker = DupeChecker({"DEFAULT": {}, "TRACKERS": {"VIETMEDIAF": {}}})
    meta_vie = Meta(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        type="WEBDL",
        category="MOVIE",
        video_codec="H.264",
        video_encode="AVC",
    )
    dupes = [
        {"name": "Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD", "size": "2 GB", "res": "1080p", "type": "WEBDL"},
        {"name": "Movie.2024.ViE.DUB.1080p.AMZN.WEB-DL.DDP5.1.H.264-DUB", "size": "2.1 GB", "res": "1080p", "type": "WEBDL"},
    ]
    # Existing Original and ViE DUB should be excluded when upload is ViE
    filtered = asyncio.run(checker.filter_dupes(dupes, meta_vie, "VIETMEDIAF"))
    assert len(filtered) == 0

    # But existing ViE should NOT be excluded
    dupes_with_vie = [
        {"name": "Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD", "size": "2 GB", "res": "1080p", "type": "WEBDL"},
    ]
    filtered_vie = asyncio.run(checker.filter_dupes(dupes_with_vie, meta_vie, "VIETMEDIAF"))
    assert len(filtered_vie) == 1


def test_vietmediaf_trumping_rule_subtitles_same_slot():
    vmf = tracker()
    meta_upload = Meta(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    dupes = [
        {
            "id": 1001,
            "name": "Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "media_info": "Text #1\nLanguage : English\n",
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    assert dupes[0]["trumpable"] is True
    assert dupes[0]["trump_reason"] == "missing_vietnamese_subtitles"


def test_vietmediaf_trumping_does_not_trump_different_audio_slot():
    vmf = tracker()
    # Upload is ViE (Voice-over), tracker is Original -> different slot, should NOT trump
    meta_upload = Meta(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    dupes = [
        {
            "id": 1002,
            "name": "Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "media_info": "Text #1\nLanguage : English\n",
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    assert dupes[0]["trumpable"] is False


def test_vietmediaf_slot_rules_trumps_when_upload_adds_vietnamese_subs():
    from src.slot_rules.trackers.vietmediaf import VietMediaFSlotRules
    from src.slot_rules.models import Verdict
    from src.slot_rules.profile import ReleaseProfile, WEBDL, H264

    rules = VietMediaFSlotRules(Meta(category="MOVIE"))
    upload = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-NEW",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi", "en"}),
    )
    existing = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"en"}),
    )

    decision = rules.evaluate(upload, [existing])
    assert decision is not None
    assert decision.verdict == Verdict.ALLOW
    assert "adds Vietnamese subtitles" in decision.why




def test_vietmediaf_trumps_bad_release_group():
    vmf = tracker()
    meta_upload = Meta(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    dupes = [
        {
            "id": 2001,
            "name": "Movie.2024.1080p.WEBRip.x264-YTS",
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    assert dupes[0]["trumpable"] is True
    assert dupes[0]["trump_reason"] == "bad_release_group"


def test_vietmediaf_trumps_fewer_vietnamese_audio_tracks():
    vmf = tracker()
    meta_upload = Meta(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
        mediainfo={
            "media": {
                "track": [
                    {"@type": "Audio", "Title": "Thuyết Minh"},
                    {"@type": "Audio", "Title": "Lồng Tiếng"},
                ]
            }
        },
    )
    dupes = [
        {
            "id": 2002,
            "name": "Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "media_info": "Audio #1\nTitle : Thuyết Minh\n\nText #1\nLanguage : Vietnamese\n",
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    assert dupes[0]["trumpable"] is True
    assert dupes[0]["trump_reason"] in ("fewer_vietnamese_audio_tracks", "missing_dual_vietnamese_audio")


def test_vietmediaf_trumping_resets_trumpable_on_different_slot():
    vmf = tracker()
    # Upload is ViE, existing is Original slot marked trumpable by tracker API
    meta_upload = Meta(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    dupes = [
        {
            "id": 2003,
            "name": "Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "trumpable": True,
            "trump_reason": "missing_vietnamese_subtitles",
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    # Must NOT trump cross-slot
    assert dupes[0]["trumpable"] is False


def test_vietmediaf_slot_rules_trumps_bad_release_group():
    from src.slot_rules.trackers.vietmediaf import VietMediaFSlotRules
    from src.slot_rules.models import Verdict
    from src.slot_rules.profile import ReleaseProfile, WEBDL, H264

    rules = VietMediaFSlotRules(Meta(category="MOVIE"))
    upload = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi"}),
    )
    existing = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-YTS",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi"}),
    )

    decision = rules.evaluate(upload, [existing])
    assert decision is not None
    assert decision.verdict == Verdict.ALLOW
    assert "bad release group" in decision.why


def test_vietmediaf_slot_rules_trumps_fewer_vietnamese_audio_tracks():
    from src.slot_rules.trackers.vietmediaf import VietMediaFSlotRules
    from src.slot_rules.models import Verdict
    from src.slot_rules.profile import ReleaseProfile, WEBDL, H264

    meta = Meta(
        category="MOVIE",
        mediainfo={
            "media": {
                "track": [
                    {"@type": "Audio", "Title": "Thuyết Minh"},
                    {"@type": "Audio", "Title": "Lồng Tiếng"},
                ]
            }
        },
    )
    rules = VietMediaFSlotRules(meta)
    upload = ReleaseProfile(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-NEW",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi"}),
    )
    existing = ReleaseProfile(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi"}),
        entry={"media_info": "Audio #1\nTitle : Thuyết Minh\n"},
    )

    decision = rules.evaluate(upload, [existing])
    assert decision is not None
    assert decision.verdict == Verdict.ALLOW
    assert "more Vietnamese audio tracks" in decision.why


def test_vietmediaf_incomplete_pack_naming():
    from src.trackers.naming import add_incomplete_pack_marker, INCOMPLETE_PACK_TRACKERS

    assert "VIETMEDIAF" in INCOMPLETE_PACK_TRACKERS
    meta = Meta(category="TV", tv_pack=True, season="1", season_pack_incomplete=True)
    orig_name = "Loki.S01.1080p.DSNP.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
    new_name = add_incomplete_pack_marker(orig_name, meta, "VIETMEDIAF")
    assert "S01.INCOMPLETE" in new_name or "S01 INCOMPLETE" in new_name


@pytest.mark.asyncio
async def test_vietmediaf_unattended_auto_trump(monkeypatch: pytest.MonkeyPatch):
    from src.uphelper import UploadHelper
    from src.slot_rules.models import SlotDecision, Verdict
    from datetime import date

    helper = UploadHelper({"DEFAULT": {"trackers": "VIETMEDIAF"}, "TRACKERS": {"VIETMEDIAF": {"api_key": "dummy"}}})
    monkeypatch.setattr(helper, "_show_slot_decision", lambda *args, **kwargs: None)

    dupes = [{
        "id": 12345,
        "name": "Movie.2024.1080p.BluRay.DTS-HD.MA.5.1-GRP",
        "trumpable": True,
        "trump_reason": "missing_vietnamese_subtitles"
    }]
    decision = SlotDecision(verdict=Verdict.ALLOW, reason="Slot allows trump", tracker="VIETMEDIAF", rules_date=date(2026, 10, 28), why="vietmediaf_trump")

    # With -ua alone: no trump report flagged
    meta_ua = Meta(unattended=True, unattended_auto=False, dupe=False, trumpable_id=12345)
    is_dupe, meta_out = await helper.dupe_check(dupes, meta_ua, "VIETMEDIAF", decision)
    assert is_dupe is False
    assert meta_out.were_trumping is False

    # With -uaa: auto-trump flagged and metadata populated
    meta_uaa = Meta(unattended=True, unattended_auto=True, dupe=False, trumpable_id=12345)
    is_dupe_b, meta_out_b = await helper.dupe_check(dupes, meta_uaa, "VIETMEDIAF", decision)
    assert is_dupe_b is False
    assert meta_out_b.were_trumping is True
    assert meta_out_b.trump_reason == "missing_vietnamese_subtitles"
    assert meta_out_b.get("VIETMEDIAF_trumpable_id") == 12345
    assert meta_out_b.get("VIETMEDIAF_trumpable_name") == "Movie.2024.1080p.BluRay.DTS-HD.MA.5.1-GRP"


    # With -uaa and Verdict.BLOCK (exact dupe on site): must NOT trump and must skip upload
    decision_block = SlotDecision(
        verdict=Verdict.BLOCK,
        reason="This exact release is already on the site.",
        tracker="VIETMEDIAF",
        rules_date=date(2026, 10, 28),
        conflicts=[{"id": 99999, "name": "Exact.Movie.1080p-GRP"}],
    )
    meta_block = Meta(
        unattended=True,
        unattended_auto=True,
        dupe=False,
        filename_match="Exact.Movie.1080p = link",
        file_count_match=1,
    )
    exact_dupes = [{"id": 99999, "name": "Exact.Movie.1080p-GRP", "trumpable": False}]
    is_dupe_c, meta_out_c = await helper.dupe_check(exact_dupes, meta_block, "VIETMEDIAF", decision_block)
    assert is_dupe_c is True
    assert meta_out_c.were_trumping is False


@pytest.mark.asyncio
async def test_vietmediaf_bad_release_groups_are_not_banned_from_upload():
    from src.trackersetup import TrackerSetup
    setup = TrackerSetup(example_config)
    meta = Meta(tag="-YTS")
    is_banned = await setup.check_banned_group("VIETMEDIAF", VietMediaF.banned_groups, meta)
    assert is_banned is False
    assert VietMediaF.banned_groups == ()
    assert "YTS" in VietMediaF.bad_release_groups


def test_vietmediaf_build_trump_report_message():
    from src.trackersetup import TrackerSetup
    from src.trackers.UNIT3D.vietmediaf import VietMediaF

    setup = TrackerSetup(example_config)
    vmf_instance = VietMediaF(example_config)

    meta = Meta(
        name="Movie.2024.1080p.BluRay.x264.ViE-TEAM",
        ua_name="Upload Assistant",
        trump_reason="missing_vietnamese_subtitles",
        VIETMEDIAF_reported_torrent_name="Movie.2024.1080p.BluRay.x264-OLD",
        screenshots_in_description=True,
    )

    msg = setup._build_trump_report_message(
        meta=meta,
        tracker="VIETMEDIAF",
        reported_torrent_id="111",
        trumping_torrent_id="222",
        tracker_instance=vmf_instance,
    )

    assert "Trump Report: Release adds Vietnamese subtitles" in msg
    assert "• Reported torrent: Movie.2024.1080p.BluRay.x264-OLD (ID: 111)" in msg
    assert "• Trumping torrent: Movie.2024.1080p.BluRay.x264.ViE-TEAM (ID: 222)" in msg
    assert "📋 Suggested deletion notice for staff to copy:" in msg
    assert "https://tracker.vietmediaf.store/torrents/222" in msg
    assert "Please consider downloading the new torrent and seeding!" in msg
    assert "(Note: User says comparison screenshots are in description)." in msg


def test_vietmediaf_build_trump_report_message_bad_group():
    from src.trackersetup import TrackerSetup
    from src.trackers.UNIT3D.vietmediaf import VietMediaF

    setup = TrackerSetup(example_config)
    vmf_instance = VietMediaF(example_config)

    meta = Meta(
        name="Movie.2024.1080p.BluRay.x264-PROPER",
        trump_reason="bad_release_group",
        VIETMEDIAF_reported_torrent_name="Movie.2024.1080p.BluRay.x264-YTS",
    )

    msg = setup._build_trump_report_message(
        meta=meta,
        tracker="VIETMEDIAF",
        reported_torrent_id="500",
        trumping_torrent_id="501",
        tracker_instance=vmf_instance,
    )

    assert "Proper encode replacing bad release group (YIFY/YTS/mSD/MeGusta/FGT)" in msg
    assert "• Reported torrent: Movie.2024.1080p.BluRay.x264-YTS (ID: 500)" in msg
    assert "• Trumping torrent: Movie.2024.1080p.BluRay.x264-PROPER (ID: 501)" in msg
    assert "proper encode replacing bad release group" in msg


def test_vietmediaf_build_trump_report_message_fewer_audio():
    from src.trackersetup import TrackerSetup
    from src.trackers.UNIT3D.vietmediaf import VietMediaF

    setup = TrackerSetup(example_config)
    vmf_instance = VietMediaF(example_config)

    meta = Meta(
        name="Movie.2024.1080p.BluRay.x264.ViE-DUB.2Audio-GRP",
        trump_reason="fewer_vietnamese_audio_tracks",
        VIETMEDIAF_reported_torrent_name="Movie.2024.1080p.BluRay.x264.ViE-DUB.1Audio-GRP",
    )

    msg = setup._build_trump_report_message(
        meta=meta,
        tracker="VIETMEDIAF",
        reported_torrent_id="600",
        trumping_torrent_id="601",
        tracker_instance=vmf_instance,
    )

    assert "Release adds more Vietnamese audio tracks" in msg
    assert "• Reported torrent: Movie.2024.1080p.BluRay.x264.ViE-DUB.1Audio-GRP (ID: 600)" in msg
    assert "• Trumping torrent: Movie.2024.1080p.BluRay.x264.ViE-DUB.2Audio-GRP (ID: 601)" in msg
    assert "release adds more Vietnamese audio tracks" in msg


@pytest.mark.asyncio
async def test_vietmediaf_get_search_urls_appends_category_id():
    vmf = tracker()
    meta_movie = Meta(category="MOVIE")
    urls = await vmf.get_search_urls(meta_movie, [("tmdbId", "8358")])
    assert any(k == "categories[]" and v == "1" for k, v in urls[0][1])

    meta_tv = Meta(category="TV")
    urls_tv = await vmf.get_search_urls(meta_tv, [("tmdbId", "8358")])
    assert any(k == "categories[]" and v == "2" for k, v in urls_tv[0][1])


def test_vietmediaf_no_cross_category_trump_movie_vs_tv():
    vmf = tracker()
    meta_upload = Meta(
        name="Cast Away 2000 1080p AMZN WEB-DL DD+ 5.1 H.264-GPRS",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    dupes = [
        {
            "id": 9198,
            "name": "Lie to Me S02 1080p DSNP WEB-DL DD+ 5.1 H.264-playWEB",
            "size": "20.0 GB",
            "trumpable": True,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    assert dupes[0]["trumpable"] is False


@pytest.mark.asyncio
async def test_dupe_checking_excludes_tv_candidate_for_movie_upload():
    from src.dupe_checking import DupeChecker

    checker = DupeChecker(example_config)
    meta = Meta(
        name="Cast Away 2000 1080p AMZN WEB-DL DD+ 5.1 H.264-GPRS",
        title="Cast Away",
        year="2000",
        resolution="1080p",
        category="MOVIE",
        type="WEBDL",
        source="WEB-DL",
        is_disc=None,
    )
    dupes = [
        {
            "id": 9198,
            "name": "Lie to Me S02 1080p DSNP WEB-DL DD+ 5.1 H.264-playWEB",
            "size": "20.0 GB",
            "res": "1080p",
            "type": "WEBDL",
            "trumpable": True,
        }
    ]
    filtered = await checker.filter_dupes(dupes, meta, "VIETMEDIAF")
    assert len(filtered) == 0
    assert meta.trumpable_id is None


@pytest.mark.asyncio
async def test_dupe_checking_records_trumpable_match_without_error():
    from src.dupe_checking import DupeChecker

    checker = DupeChecker(example_config)
    meta = Meta(
        name="Hostel 2006 Unrated 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-FraMeSToR",
        title="Hostel",
        year="2006",
        resolution="2160p",
        category="MOVIE",
        type="REMUX",
        source="BluRay",
        hdr="DV HDR",
        is_disc=None,
    )
    dupes = [
        {
            "id": 12902,
            "name": "Hostel 2006 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-MALUS",
            "size": "50.0 GB",
            "res": "2160p",
            "type": "REMUX",
            "trumpable": True,
        }
    ]
    filtered = await checker.filter_dupes(dupes, meta, "VIETMEDIAF")
    assert len(filtered) == 1
    assert meta.trumpable_id == 12902
    assert meta.trumpable_name == "Hostel 2006 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-MALUS"
    assert meta.get("VIETMEDIAF_trumpable_id") == 12902
    assert meta.get("VIETMEDIAF_trumpable_name") == "Hostel 2006 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-MALUS"
    assert meta.get("VIETMEDIAF_matched_download") is None


@pytest.mark.asyncio
async def test_trumpable_candidate_does_not_clobber_cross_seed_match():
    from src.dupe_checking import DupeChecker
    from src.uphelper import UploadHelper

    filename = "Hostel.2006.Unrated.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.REMUX-FraMeSToR.mkv"
    checker = DupeChecker(example_config)
    meta = Meta(
        name="Hostel 2006 Unrated 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-FraMeSToR",
        title="Hostel",
        year="2006",
        resolution="2160p",
        category="MOVIE",
        type="REMUX",
        source="BluRay",
        hdr="DV HDR",
        is_disc=None,
        filelist=[f"/downloads/{filename}"],
    )
    dupes = [
        {
            "id": 12902,
            "name": "Hostel 2006 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-MALUS",
            "size": "50.0 GB",
            "res": "2160p",
            "type": "REMUX",
            "download": "https://tracker.vietmediaf.store/torrents/download/12902.torrent",
            "trumpable": True,
        },
        {
            "id": 12890,
            "name": "Hostel 2006 Unrated 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-FraMeSToR",
            "size": "62.3 GB",
            "res": "2160p",
            "type": "REMUX",
            "download": "https://tracker.vietmediaf.store/torrents/download/12890.torrent",
            "files": [filename],
            "file_count": 1,
            "trumpable": False,
        },
    ]
    filtered = await checker.filter_dupes(dupes, meta, "VIETMEDIAF")
    # Both are non-excluded dupes
    assert len(filtered) == 2
    # Trumpable info correctly points to MALUS
    assert meta.trumpable_id == 12902
    assert meta.get("VIETMEDIAF_trumpable_id") == 12902
    # Matched cross-seed info correctly points to FraMeSToR, NOT MALUS!
    assert meta.get("VIETMEDIAF_matched_id") == 12890
    assert meta.get("VIETMEDIAF_matched_download") == "https://tracker.vietmediaf.store/torrents/download/12890.torrent"

    # Now verify dupe_check in uphelper selects FraMeSToR for cross-seed
    helper = UploadHelper(example_config)
    meta.unattended = True
    is_dupe, updated_meta = await helper.dupe_check(filtered, meta, "VIETMEDIAF")
    assert meta.get("VIETMEDIAF_cross_seed") == "https://tracker.vietmediaf.store/torrents/download/12890.torrent"





def test_vietmediaf_trumping_rule_subtitles_requires_mediainfo():
    vmf = tracker()
    meta_upload = Meta(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        subtitle_languages=["vi"],
    )
    # Target entry has NO media_info (e.g. API fetch failed)
    dupes = [
        {
            "id": 1001,
            "name": "Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "media_info": None,
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    # Must NOT trump because MediaInfo is unavailable
    assert dupes[0]["trumpable"] is False


def test_vietmediaf_trumping_rule_audio_count_requires_mediainfo():
    vmf = tracker()
    meta_upload = Meta(
        name="Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
        resolution="1080p",
        category="MOVIE",
        mediainfo={
            "media": {
                "track": [
                    {"@type": "Audio", "Title": "Thuyet Minh"},
                    {"@type": "Audio", "Title": "Long Tieng"},
                ]
            }
        },
    )
    # Target entry has no media_info
    dupes = [
        {
            "id": 1002,
            "name": "Movie.2024.ViE.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
            "media_info": None,
            "trumpable": False,
        }
    ]
    vmf._apply_vietnamese_trump_rules(meta_upload, dupes)
    # Must NOT trump because MediaInfo is unavailable
    assert dupes[0]["trumpable"] is False


def test_vietmediaf_slot_rules_subtitles_requires_parsed_existing_subtitles():
    from src.slot_rules.trackers.vietmediaf import VietMediaFSlotRules
    from src.slot_rules.models import Verdict
    from src.slot_rules.profile import ReleaseProfile, WEBDL, H264

    rules = VietMediaFSlotRules(Meta(category="MOVIE"))
    upload = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-NEW",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=frozenset({"vi", "en"}),
    )
    # Existing release MediaInfo was missing, so subtitle_languages is None (not frozenset)
    existing = ReleaseProfile(
        name="Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-OLD",
        resolution="1080p",
        kind=WEBDL,
        codec=H264,
        subtitle_languages=None,
    )

    decision = rules.evaluate(upload, [existing])
    assert decision is not None
    # Must NOT allow trumping without confirmed MediaInfo
    assert decision.verdict == Verdict.BLOCK


@pytest.mark.asyncio
async def test_unit3d_fill_torrent_details_retries_and_marks_failed():
    import httpx
    vmf = tracker()
    entries = [{"id": 999, "media_info": None}]
    
    attempts = 0
    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("Connection timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await vmf._fill_torrent_details(client, {}, entries)

    # Must have attempted 3 times
    assert attempts == 3
    # Must mark details_failed=True
    assert entries[0].get("details_failed") is True


@pytest.mark.asyncio
async def test_season_pack_exact_match_not_clobbered_by_competing_season_pack():
    from src.dupe_checking import DupeChecker
    from src.uphelper import UploadHelper
    from src.slot_rules.base import SlotDecision, Verdict

    filename = "House.of.Cards.US.S05E01.2160p.NF.WEB-DL.DTS-HD.MA.5.1.H.265-Kitsune.mkv"
    checker = DupeChecker(example_config)
    meta = Meta(
        name="House of Cards S05 2160p NF WEB-DL DTS-HD MA 5.1 H.265-Kitsune",
        title="House of Cards",
        year="2013",
        season="S05",
        episode="",
        resolution="2160p",
        category="TV",
        type="WEBDL",
        source="Web",
        is_disc=None,
        filelist=[f"/downloads/{filename}"],
    )
    dupes = [
        {
            "id": 101,
            "name": "House of Cards S05 2160p NF WEB-DL DTS-HD MA 5.1 H.265-Kitsune",
            "size": "118.8 GB",
            "res": "2160p",
            "type": "WEBDL",
            "download": "https://tracker.vietmediaf.store/torrents/download/101.torrent",
            "files": [filename],
            "file_count": 1,
            "trumpable": False,
        },
        {
            "id": 102,
            "name": "House of Cards US S05 2160p Netflix WEB-DL DD5.1 HEVC-TrollUHD",
            "size": "118.8 GB",
            "res": "2160p",
            "type": "WEBDL",
            "download": "https://tracker.vietmediaf.store/torrents/download/102.torrent",
            "files": ["House.of.Cards.US.S05E01.2160p.Netflix.WEB-DL.DD5.1.HEVC-TrollUHD.mkv"],
            "file_count": 1,
            "trumpable": False,
        },
    ]

    filtered = await checker.filter_dupes(dupes, meta, "VIETMEDIAF")
    assert len(filtered) == 2
    # Verify TrollUHD was NOT treated as an episode to trump
    assert not meta.get("VIETMEDIAF_matched_episode_ids")
    # Verify matched download and name point strictly to Kitsune, not TrollUHD!
    assert meta.get("VIETMEDIAF_matched_id") == 101
    assert meta.get("VIETMEDIAF_matched_name") == "House of Cards S05 2160p NF WEB-DL DTS-HD MA 5.1 H.265-Kitsune"
    assert meta.get("VIETMEDIAF_matched_download") == "https://tracker.vietmediaf.store/torrents/download/101.torrent"

    # Now verify dupe_check in uphelper sets cross_seed to Kitsune
    helper = UploadHelper(example_config)
    meta.unattended = True
    from datetime import date
    slot_decision = SlotDecision(
        verdict=Verdict.BLOCK,
        reason="This exact release is already on the site.",
        tracker="VIETMEDIAF",
        rules_date=date(2026, 10, 15),
        conflicts=[dupes[0]],
    )
    is_dupe, updated_meta = await helper.dupe_check(filtered, meta, "VIETMEDIAF", slot_decision)
    assert meta.get("VIETMEDIAF_cross_seed") == "https://tracker.vietmediaf.store/torrents/download/101.torrent"


@pytest.mark.asyncio
async def test_vietmediaf_search_existing_retains_tv_episodes(monkeypatch):
    from src.slot_rules.registry import evaluate_slots
    from src.slot_rules.models import Verdict

    vmf = tracker()
    episode_entry = {
        "id": 501,
        "name": "Stick S02E01 2160p ATVP WEB-DL Dual-Audio DD 5.1 DV HDR10+ H.265-BiOMA",
        "size": "5.2 GB",
        "res": "2160p",
        "type": "WEBDL",
        "download": "https://tracker.vietmediaf.store/torrents/download/501.torrent",
        "files": ["Stick.S02E01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265.DUAL-BiOMA.mkv"],
        "file_count": 1,
        "trumpable": False,
    }
    multi_ep_entry = {
        "id": 502,
        "name": "Stick S02E01E02 2160p ATVP WEB-DL DD 5.1 H.265-BiOMA",
        "size": "10.1 GB",
        "res": "2160p",
        "type": "WEBDL",
        "download": "https://tracker.vietmediaf.store/torrents/download/502.torrent",
        "files": [],
        "file_count": 1,
        "trumpable": False,
    }
    movie_collision_entry = {
        "id": 503,
        "name": "Stick 1985 2160p WEB-DL DD 5.1 H.265-GROUP",
        "size": "12.0 GB",
        "res": "2160p",
        "type": "WEBDL",
        "download": "https://tracker.vietmediaf.store/torrents/download/503.torrent",
        "files": [],
        "file_count": 1,
        "trumpable": False,
    }

    async def fake_super_search_existing(self, meta):
        all_results = [dict(episode_entry), dict(multi_ep_entry), dict(movie_collision_entry)]
        meta["VIETMEDIAF_title_releases"] = [dict(episode_entry), dict(multi_ep_entry), dict(movie_collision_entry)]
        return all_results

    monkeypatch.setattr("src.trackers.UNIT3D.UNIT3D.search_existing", fake_super_search_existing)

    tv_meta = Meta(
        name="Stick S02E01 2160p ATVP WEB-DL Dual-Audio DD 5.1 DV HDR10+ H.265-BiOMA",
        title="Stick",
        year="2025",
        season="S02",
        episode="E01",
        resolution="2160p",
        category="TV",
        type="WEBDL",
        source="Web",
        is_disc=None,
        filelist=["/downloads/Stick.S02E01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265.DUAL-BiOMA.mkv"],
    )

    dupes = await vmf.search_existing(tv_meta)
    assert [d["id"] for d in dupes] == [501, 502]
    assert [d["id"] for d in tv_meta["VIETMEDIAF_title_releases"]] == [501, 502]

    decision = await evaluate_slots(dupes, tv_meta, "VIETMEDIAF", example_config)
    assert decision is not None
    assert decision.verdict == Verdict.BLOCK
    assert decision.reason == "This exact release is already on the site."

    movie_meta = Meta(
        name="Stick 1985 2160p WEB-DL DD 5.1 H.265-GROUP",
        title="Stick",
        year="1985",
        resolution="2160p",
        category="MOVIE",
        type="WEBDL",
        source="Web",
        is_disc=None,
    )
    movie_dupes = await vmf.search_existing(movie_meta)
    assert [d["id"] for d in movie_dupes] == [503]
    assert [d["id"] for d in movie_meta["VIETMEDIAF_title_releases"]] == [503]


@pytest.mark.asyncio
async def test_vietmediaf_repack_season_pack_triggers_trump_report():
    from src.dupe_checking import DupeChecker
    from src.slot_rules.models import Verdict
    from src.slot_rules.registry import evaluate_slots
    from src.trackersetup import TrackerSetup
    from src.uphelper import UploadHelper

    existing_entry = {
        "id": 777,
        "name": "True Beauty 2024 AKA Yeosin Gangnim S01 1080p CR WEB-DL Dual-Audio DD+ 2.0 H.264-Kitsune",
        "size": "14.5 GB",
        "res": "1080p",
        "type": "WEBDL",
        "download": "https://tracker.vietmediaf.store/torrents/download/777.torrent",
        "files": ["True.Beauty.2024.S01E01.1080p.CR.WEB-DL.DUAL.DDP2.0.H.264-Kitsune.mkv"],
        "file_count": 13,
        "trumpable": False,
    }
    dupes = [dict(existing_entry)]

    meta = Meta(
        name="True Beauty 2024 S01 REPACK 1080p CR WEB-DL Dual-Audio DD+ 2.0 H.264-Kitsune",
        clean_name="True Beauty 2024 S01 REPACK 1080p CR WEB-DL Dual-Audio DD+ 2.0 H.264-Kitsune",
        ua_name="Upload-Assistant",
        uuid="True.Beauty.2024.S01.REPACK.1080p.CR.WEB-DL.DUAL.DDP2.0.H.264-Kitsune",
        title="True Beauty",
        year="2024",
        season="S01",
        episode="",
        tv_pack=1,
        resolution="1080p",
        category="TV",
        type="WEBDL",
        source="Web",
        video_codec="H.264",
        tag="-Kitsune",
        repack="REPACK",
        is_disc=None,
        unattended=True,
        unattended_auto=False,
        filelist=["/downloads/True.Beauty.2024.S01.REPACK.1080p.CR.WEB-DL.DUAL.DDP2.0.H.264-Kitsune/True.Beauty.2024.S01E01.REPACK.1080p.CR.WEB-DL.DUAL.DDP2.0.H.264-Kitsune.mkv"],
        VIETMEDIAF_title_releases=[dict(existing_entry)],
    )

    slot_decision = await evaluate_slots(dupes, meta, "VIETMEDIAF", example_config)
    assert slot_decision is not None
    assert slot_decision.verdict == Verdict.ALLOW
    assert "trump" in slot_decision.reason.lower()
    assert len(slot_decision.conflicts) == 1
    assert slot_decision.conflicts[0]["id"] == 777

    checker = DupeChecker(example_config)
    filtered_dupes = await checker.filter_dupes(dupes, meta, "VIETMEDIAF")
    # filter_dupes excludes the non-REPACK release because of missing 'repack'
    assert filtered_dupes == []

    helper = UploadHelper(example_config)
    is_dupe, updated_meta = await helper.dupe_check(filtered_dupes, meta, "VIETMEDIAF", slot_decision)
    assert is_dupe is False
    assert updated_meta.were_trumping is True
    assert updated_meta.trump_reason == "repack"
    assert updated_meta.get("VIETMEDIAF_trumpable_id") == 777
    assert updated_meta.get("VIETMEDIAF_trumpable_name") == existing_entry["name"]

    setup = TrackerSetup(example_config)
    vmf = tracker()
    msg = setup._build_trump_report_message(
        updated_meta,
        "VIETMEDIAF",
        "777",
        "888",
        vmf,
    )
    assert "REPACK/PROPER replacing original release" in msg
    assert "True Beauty 2024 AKA Yeosin Gangnim S01 1080p CR WEB-DL Dual-Audio DD+ 2.0 H.264-Kitsune (ID: 777)" in msg
    assert "True Beauty 2024 S01 REPACK 1080p CR WEB-DL Dual-Audio DD+ 2.0 H.264-Kitsune (ID: 888)" in msg


@pytest.mark.asyncio
async def test_vietmediaf_streaming_service_video_bitrate_trump_and_anti_bloat_guard():
    from src.slot_rules.models import Verdict
    from src.slot_rules.registry import evaluate_slots

    existing_nf = {
        "id": 901,
        "name": "Sample Movie 2026 1080p NF WEB-DL DD+ 5.1 H.264-GROUP",
        "size": "3.2 GB",
        "res": "1080p",
        "type": "WEBDL",
        "trumpable": False,
        "media_info": (
            "Video\n"
            "Format : AVC\n"
            "Bit rate : 3 500 kb/s\n\n"
            "Audio\n"
            "Format : E-AC-3\n"
            "Language : English\n\n"
            "Text\n"
            "Format : UTF-8\n"
            "Language : Vietnamese\n"
        ),
    }

    # Case 1: Clean AMZN WEB-DL with higher video bitrate (8000 kbps vs 3500 kbps) -> ALLOW trump
    clean_amzn_meta = Meta(
        name="Sample Movie 2026 1080p AMZN WEB-DL DD+ 5.1 H.264-BETTER",
        clean_name="Sample Movie 2026 1080p AMZN WEB-DL DD+ 5.1 H.264-BETTER",
        title="Sample Movie",
        year="2026",
        resolution="1080p",
        category="MOVIE",
        type="WEBDL",
        source="Web",
        service="AMZN",
        video_codec="H.264",
        original_language="en",
        audio_languages=["English"],
        subtitle_languages=["Vietnamese"],
        mediainfo={
            "media": {
                "track": [
                    {"@type": "General", "OverallBitRate": "8640000"},
                    {"@type": "Video", "Format": "AVC", "BitRate": "8000000"},
                    {"@type": "Audio", "Format": "E-AC-3", "Language": "en", "Title": "English"},
                    {"@type": "Text", "Format": "UTF-8", "Language": "vi", "Title": "Vietnamese"},
                ]
            }
        },
        VIETMEDIAF_title_releases=[dict(existing_nf)],
    )

    decision_clean = await evaluate_slots([dict(existing_nf)], clean_amzn_meta, "VIETMEDIAF", example_config)
    assert decision_clean is not None
    assert decision_clean.verdict == Verdict.ALLOW
    assert "higher video bitrate" in decision_clean.why.lower()

    # Case 2: Bloated AMZN WEB-DL (contains extra Russian audio track) -> BLOCKED from trumping clean NF WEB-DL!
    bloated_amzn_meta = Meta(
        name="Sample Movie 2026 1080p AMZN WEB-DL Multi DD+ 5.1 H.264-BLOAT",
        clean_name="Sample Movie 2026 1080p AMZN WEB-DL Multi DD+ 5.1 H.264-BLOAT",
        title="Sample Movie",
        year="2026",
        resolution="1080p",
        category="MOVIE",
        type="WEBDL",
        source="Web",
        service="AMZN",
        video_codec="H.264",
        original_language="en",
        audio_languages=["English", "Russian"],
        subtitle_languages=["Vietnamese"],
        mediainfo={
            "media": {
                "track": [
                    {"@type": "General", "OverallBitRate": "9280000"},
                    {"@type": "Video", "Format": "AVC", "BitRate": "8000000"},
                    {"@type": "Audio", "Format": "E-AC-3", "Language": "en", "Title": "English"},
                    {"@type": "Audio", "Format": "E-AC-3", "Language": "ru", "Title": "Russian Dub"},
                    {"@type": "Text", "Format": "UTF-8", "Language": "vi", "Title": "Vietnamese"},
                ]
            }
        },
        VIETMEDIAF_title_releases=[dict(existing_nf)],
    )

    decision_bloated = await evaluate_slots([dict(existing_nf)], bloated_amzn_meta, "VIETMEDIAF", example_config)
    assert decision_bloated is not None
    assert decision_bloated.verdict == Verdict.BLOCK
    assert "bloated" in decision_bloated.why.lower()



