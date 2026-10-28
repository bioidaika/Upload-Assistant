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
