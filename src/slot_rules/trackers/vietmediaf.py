# Upload Assistant © 2026 Audionut & wastaken7 — Licensed under UAPL v1.0
"""VietMediaF slot rules: Original, ViE (Voice-over), and ViE DUB (Dubbed) slots."""

from collections.abc import Sequence

from src.slot_rules.base import SlotKey, SlotRules, slot_label
from src.slot_rules.models import SlotDecision, Verdict
from src.slot_rules.profile import WEBDL, ReleaseProfile
from src.trackers.UNIT3D.vietmediaf import VietMediaF


class VietMediaFSlotRules(SlotRules):
    TRACKER = "VIETMEDIAF"

    def audio_slot(self, profile: ReleaseProfile) -> str:
        tag = VietMediaF._existing_audio_tag(profile.name)
        if tag is None and self.meta is not None and getattr(profile, "exact", False) is False:
            meta_name = getattr(self.meta, "name", "") or ""
            meta_clean = getattr(self.meta, "clean_name", "") or ""
            if profile.name in (meta_name, meta_clean) or not tag:
                tag = VietMediaF._metadata_audio_tag(self.meta)
        if tag == "dub":
            return "ViE DUB"
        if tag == "vie":
            return "ViE"
        return "Original"

    def key(self, profile: ReleaseProfile) -> SlotKey:
        res = self.require(profile.resolution, "resolution")
        kind = self.require(profile.kind, "release type")
        codec = self.require(profile.codec, "video codec")
        audio = self.audio_slot(profile)
        return (res, kind, codec, audio, profile.edition)

    def _upload_has_audio_bloat(self, upload: ReleaseProfile) -> bool:
        if self.meta is not None and VietMediaF._has_audio_bloat_meta(self.meta):
            return True
        if upload.audio_languages:
            orig_raw = getattr(self.meta, "original_language", None) if self.meta is not None else None
            if isinstance(orig_raw, list):
                orig_raw = orig_raw[0] if orig_raw else None
            orig_code = VietMediaF._normalize_lang_code(str(orig_raw)) if orig_raw else None
            if orig_code and orig_code != "und":
                allowed = {"vi", orig_code}
                return not set(upload.audio_languages).issubset(allowed)
            non_vi = set(upload.audio_languages) - {"vi"}
            return len(non_vi) >= 2
        return False

    def _existing_has_audio_bloat(self, existing: ReleaseProfile) -> bool:
        orig_raw = getattr(self.meta, "original_language", None) if self.meta is not None else None
        if isinstance(orig_raw, list):
            orig_raw = orig_raw[0] if orig_raw else None
        orig_str = str(orig_raw) if orig_raw else None
        existing_entry = dict(existing.entry) if existing.entry else {}
        if VietMediaF._has_audio_bloat_entry(existing_entry, orig_str):
            return True
        if existing.audio_languages:
            orig_code = VietMediaF._normalize_lang_code(orig_str) if orig_str else None
            if orig_code and orig_code != "und":
                allowed = {"vi", orig_code}
                return not set(existing.audio_languages).issubset(allowed)
            non_vi = set(existing.audio_languages) - {"vi"}
            return len(non_vi) >= 2
        return False

    def vietmediaf_trump(self, upload: ReleaseProfile, existing: ReleaseProfile) -> tuple[Verdict, str]:
        upload_has_bloat = self._upload_has_audio_bloat(upload)
        existing_has_bloat = self._existing_has_audio_bloat(existing)

        # 0. Anti-bloat guard: A bloated-audio upload can NEVER trump a clean release
        if upload_has_bloat and not existing_has_bloat:
            return Verdict.BLOCK, "Your upload contains bloated or undetermined audio tracks while the existing release has clean audio."

        # 1. Repack check
        repack_verdict, repack_reason = self.default_trump(upload, existing)
        if repack_verdict is Verdict.ALLOW:
            return repack_verdict, repack_reason

        # 2. Bad release group check:
        # If existing release is from a bad release group and upload is a proper encode
        if VietMediaF._is_bad_release_group(existing.name) and not VietMediaF._is_bad_release_group(upload.name):
            return Verdict.ALLOW, "Existing release is from a bad release group while your upload is a proper release."

        # 3. Vietnamese subtitles check:
        # If upload has Vietnamese subtitles and existing release lacks them
        upload_vi_sub = bool(upload.subtitle_languages and "vi" in upload.subtitle_languages)
        if not upload_vi_sub and self.meta is not None:
            upload_vi_sub = VietMediaF._has_vietnamese_subtitles_meta(self.meta)
        existing_vi_sub = bool(existing.subtitle_languages and "vi" in existing.subtitle_languages)
        if not existing_vi_sub and existing.entry:
            existing_vi_sub = VietMediaF._has_vietnamese_subtitles_entry(dict(existing.entry))
        if upload_vi_sub and not existing_vi_sub:
            # Safety check: Cannot safely confirm existing release lacks Vietnamese subtitles if MediaInfo was not parsed
            if existing.subtitle_languages is not None or (existing.entry and VietMediaF._entry_has_mediainfo(dict(existing.entry))):
                return Verdict.ALLOW, "Your release adds Vietnamese subtitles which the existing release lacks."

        # 4. Vietnamese audio count check (within ViE / ViE DUB slots):
        audio_slot = self.audio_slot(upload)
        existing_entry = dict(existing.entry) if existing.entry else {}
        upload_vi_count = 0
        if self.meta is not None:
            upload_vi_count = VietMediaF._count_vietnamese_audio_tracks_meta(self.meta)
        if upload.entry:
            upload_vi_count = max(upload_vi_count, VietMediaF._count_vietnamese_audio_tracks_entry(dict(upload.entry)))
        if upload_vi_count == 0 and VietMediaF._existing_audio_tag(upload.name):
            upload_vi_count = 1

        existing_vi_count = 0
        if existing.entry:
            existing_vi_count = VietMediaF._count_vietnamese_audio_tracks_entry(existing_entry)
        if existing_vi_count == 0 and VietMediaF._existing_audio_tag(existing.name):
            existing_vi_count = 1

        if audio_slot in ("ViE", "ViE DUB"):
            # Safety check: Cannot safely compare Vietnamese audio tracks if existing release MediaInfo is unavailable
            if existing.audio_languages is not None or VietMediaF._entry_has_mediainfo(existing_entry):
                if upload_vi_count > existing_vi_count:
                    return (
                        Verdict.ALLOW,
                        f"Your release has more Vietnamese audio tracks ({upload_vi_count}) than the existing release ({existing_vi_count}).",
                    )

        # 5. Different Streaming Service Video Bitrate Upgrade Trump (WEB-DL only, clean audio required)
        upload_srv = VietMediaF._extract_streaming_service(upload.name, upload.provider)
        existing_srv = VietMediaF._extract_streaming_service(existing.name, existing.provider)
        if (
            not upload_has_bloat
            and not VietMediaF._is_bad_release_group(upload.name)
            and upload.kind == WEBDL
            and existing.kind == WEBDL
            and upload_srv
            and existing_srv
            and upload_srv != existing_srv
            and (upload_vi_sub or not existing_vi_sub)
            and upload_vi_count >= existing_vi_count
        ):
            upload_kbps = (VietMediaF._video_bitrate_kbps_meta(self.meta) if self.meta is not None else None) or upload.video_bitrate_kbps
            existing_kbps = VietMediaF._video_bitrate_kbps_entry(existing_entry) or existing.video_bitrate_kbps
            if upload_kbps and existing_kbps and upload_kbps >= int(existing_kbps * 1.20) and (upload_kbps - existing_kbps) >= 500:
                return (
                    Verdict.ALLOW,
                    f"Your {upload_srv} WEB-DL has a significantly higher video bitrate ({upload_kbps} kbps vs {existing_kbps} kbps) than the existing {existing_srv} WEB-DL.",
                )

        return Verdict.BLOCK, ""

    def decide(self, upload: ReleaseProfile, existing: Sequence[ReleaseProfile]) -> SlotDecision | None:
        upload_key = self.key(upload)
        label = slot_label(upload_key)
        return self.by_slot(
            upload,
            existing,
            self.key,
            label,
            trump=self.vietmediaf_trump,
            notes=["VietMediaF allows Original audio, ViE (Voice-over), and ViE DUB to coexist in separate slots."],
        )
