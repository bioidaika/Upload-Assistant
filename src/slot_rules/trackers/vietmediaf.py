# Upload Assistant © 2026 Audionut & wastaken7 — Licensed under UAPL v1.0
"""VietMediaF slot rules: Original, ViE (Voice-over), and ViE DUB (Dubbed) slots."""

from collections.abc import Sequence

from src.slot_rules.base import SlotKey, SlotRules, slot_label
from src.slot_rules.models import SlotDecision, Verdict
from src.slot_rules.profile import ReleaseProfile
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

    @classmethod
    def vietmediaf_trump(cls, upload: ReleaseProfile, existing: ReleaseProfile) -> tuple[Verdict, str]:
        # 1. Repack check
        repack_verdict, repack_reason = cls.default_trump(upload, existing)
        if repack_verdict is Verdict.ALLOW:
            return repack_verdict, repack_reason

        # 2. Vietnamese subtitles check:
        # If upload has Vietnamese subtitles and existing release lacks them
        upload_vi_sub = bool(upload.subtitle_languages and "vi" in upload.subtitle_languages)
        existing_vi_sub = bool(existing.subtitle_languages and "vi" in existing.subtitle_languages)
        if upload_vi_sub and not existing_vi_sub:
            return Verdict.ALLOW, "Your release adds Vietnamese subtitles which the existing release lacks."

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
