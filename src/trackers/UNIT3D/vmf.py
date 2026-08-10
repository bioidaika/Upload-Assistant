# Upload Assistant © 2026 Audionut & wastaken7 — Licensed under UAPL v1.0
import re
import unicodedata
from typing import Any, cast

from src.meta import Meta
from src.trackers.UNIT3D import UNIT3D


class VietMediaF(UNIT3D):
    """VietMediaF's UNIT3D API adapter."""

    tracker = "VMF"
    display_name = "VietMediaF"
    base_url = "https://tracker.vietmediaf.store"
    banned_groups: tuple[str, ...] = ()
    id_url = f"{base_url}/api/torrents/"
    upload_url = f"{base_url}/api/torrents/upload"
    search_url = f"{base_url}/api/torrents/filter"
    torrent_url = f"{base_url}/torrents/"
    supported_categories = ("MOVIE", "TV")
    tracker_urls = (base_url,)

    _vmf_tag_pattern = re.compile(r"(?<![A-Za-z0-9])ViE(?:[ .-]+(?P<dub>(?i:DUB)))?(?![A-Za-z0-9])")
    _resolution_pattern = re.compile(r"(?<![A-Za-z0-9])(?:8640p|4320p|2160p|1440p|1080[pi]|720p|576[pi]|480[pi]|8K|4K)(?![A-Za-z0-9])", re.IGNORECASE)
    _source_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:UHD[ .-]?BluRay|Blu[ .-]?Ray|WEB(?:[ .-]?(?:DL|Rip))?|HDTV|DVD|REMUX)(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    _dub_title_tokens = (re.compile(r"\blong\s+tieng\b"), re.compile(r"\b(?:uslt|vnlt)\b"))
    _voice_over_title_tokens = (re.compile(r"\bthuyet\s+minh\b"), re.compile(r"\btm\b"))
    _vietnamese_language_tokens = frozenset({"vi", "vie", "vietnamese"})

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config, tracker_name="VMF")

    @staticmethod
    def _normalized_words(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold()).replace("đ", "d")
        ascii_text = "".join(character for character in normalized if not unicodedata.combining(character))
        return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()

    @classmethod
    def _extract_audio_titles(cls, meta: Meta) -> list[str]:
        mediainfo = meta.mediainfo
        if not isinstance(mediainfo, dict):
            return []

        media = mediainfo.get("media")
        if not isinstance(media, dict):
            return []
        media = cast(dict[str, Any], media)

        raw_tracks = media.get("track", [])
        if not isinstance(raw_tracks, list):
            return []
        raw_tracks = cast(list[Any], raw_tracks)

        titles: list[str] = []
        for raw_track in raw_tracks:
            if not isinstance(raw_track, dict):
                continue
            track = cast(dict[str, Any], raw_track)
            if str(track.get("@type", "")).casefold() != "audio":
                continue
            for key, value in track.items():
                normalized_key = str(key).casefold()
                if (normalized_key == "title" or normalized_key.startswith("title_string")) and isinstance(value, str) and value.strip():
                    titles.append(value.strip())

        return list(dict.fromkeys(titles))

    @classmethod
    def _metadata_audio_tag(cls, meta: Meta) -> str | None:
        normalized_titles = [cls._normalized_words(title) for title in cls._extract_audio_titles(meta)]
        if any(pattern.search(title) for title in normalized_titles for pattern in cls._dub_title_tokens):
            return "dub"
        if any(pattern.search(title) for title in normalized_titles for pattern in cls._voice_over_title_tokens):
            return "vie"

        raw_languages = meta.audio_languages or []
        languages = [raw_languages] if isinstance(raw_languages, str) else raw_languages
        for language in languages:
            if not isinstance(language, str):
                continue
            if cls._vietnamese_language_tokens.intersection(cls._normalized_words(language).split()):
                return "vie"
        return None

    @classmethod
    def _existing_audio_tag(cls, name: str) -> str | None:
        matches = list(cls._vmf_tag_pattern.finditer(name))
        if any(match.group("dub") for match in matches):
            return "dub"
        if matches:
            return "vie"
        return None

    @classmethod
    def _remove_existing_audio_tags(cls, name: str) -> str:
        cleaned = name
        matches = list(cls._vmf_tag_pattern.finditer(name))

        # Work right-to-left so each original match offset remains valid while
        # adjacent separators are collapsed into one canonical gap.
        for match in reversed(matches):
            left = cleaned[: match.start()]
            right = cleaned[match.end() :]
            left_separator_match = re.search(r"[\s.-]+$", left)
            right_separator_match = re.match(r"[\s.-]+", right)
            left_separator = left_separator_match.group() if left_separator_match else ""
            right_separator = right_separator_match.group() if right_separator_match else ""
            left_content = left[: len(left) - len(left_separator)] if left_separator else left
            right_content = right[len(right_separator) :]

            replacement = ""
            if left_content and right_content:
                technical_suffix = cls._resolution_pattern.match(right_content) or cls._source_pattern.match(right_content)
                if "-" in right_separator and technical_suffix is None:
                    # A non-technical suffix after a hyphen is the release group.
                    replacement = "-"
                elif "." in left_separator or "." in right_separator:
                    replacement = "."
                elif any(character.isspace() for character in left_separator + right_separator):
                    replacement = " "
                else:
                    replacement = "." if "." in name and " " not in name else " "

            start = match.start() - len(left_separator)
            end = match.end() + len(right_separator)
            cleaned = f"{cleaned[:start]}{replacement}{cleaned[end:]}"

        return cleaned.strip().strip(".-").strip()

    @staticmethod
    def _last_match_start(pattern: re.Pattern[str], name: str) -> int | None:
        matches = list(pattern.finditer(name))
        return matches[-1].start() if matches else None

    @classmethod
    def _resolution_anchor(cls, name: str, resolution: str) -> int | None:
        if resolution:
            resolution_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(resolution)}(?![A-Za-z0-9])", re.IGNORECASE)
            anchor = cls._last_match_start(resolution_pattern, name)
            if anchor is not None:
                return anchor
        return cls._last_match_start(cls._resolution_pattern, name)

    @classmethod
    def _source_anchor(cls, name: str, source: str) -> int | None:
        if source:
            source_parts = [re.escape(part) for part in re.split(r"[ .]+", source.strip()) if part]
            if source_parts:
                source_expression = r"[ .]+".join(source_parts)
                source_pattern = re.compile(rf"(?<![A-Za-z0-9]){source_expression}(?![A-Za-z0-9])", re.IGNORECASE)
                anchor = cls._last_match_start(source_pattern, name)
                if anchor is not None:
                    return anchor
        return cls._last_match_start(cls._source_pattern, name)

    @staticmethod
    def _release_separator(name: str, anchor: int | None = None) -> str:
        if anchor is not None and anchor > 0:
            preceding_character = name[anchor - 1]
            if preceding_character == ".":
                return "."
            if preceding_character.isspace():
                return " "
        return "." if "." in name and " " not in name else " "

    @staticmethod
    def _group_anchor(name: str, tag: str) -> int | None:
        group = tag.strip()
        while group.startswith("-"):
            group = group[1:].strip()
        if not group:
            return None
        match = re.search(rf"-{re.escape(group)}$", name, re.IGNORECASE)
        return match.start() if match else None

    @classmethod
    def _insert_audio_tag(cls, name: str, tag_kind: str, meta: Meta) -> str:
        resolution_anchor = cls._resolution_anchor(name, meta.resolution or "")
        source_anchor = cls._source_anchor(name, meta.source or "") if resolution_anchor is None else None
        technical_anchor = resolution_anchor if resolution_anchor is not None else source_anchor

        if technical_anchor is not None:
            separator = cls._release_separator(name, technical_anchor)
            rendered_tag = f"ViE{separator}DUB" if tag_kind == "dub" else "ViE"
            if technical_anchor > 0 and (name[technical_anchor - 1] == "." or name[technical_anchor - 1].isspace()):
                return f"{name[:technical_anchor]}{rendered_tag}{separator}{name[technical_anchor:]}"
            prefix_separator = separator if technical_anchor > 0 else ""
            return f"{name[:technical_anchor]}{prefix_separator}{rendered_tag}{separator}{name[technical_anchor:]}"

        group_anchor = cls._group_anchor(name, meta.tag or "")
        if group_anchor is not None:
            separator = cls._release_separator(name, group_anchor)
            rendered_tag = f"ViE{separator}DUB" if tag_kind == "dub" else "ViE"
            return f"{name[:group_anchor]}{separator}{rendered_tag}{name[group_anchor:]}"

        separator = cls._release_separator(name)
        rendered_tag = f"ViE{separator}DUB" if tag_kind == "dub" else "ViE"
        return f"{name}{separator}{rendered_tag}" if name else rendered_tag

    async def get_name(self, meta: Meta) -> dict[str, str]:
        name = meta.name
        metadata_tag = self._metadata_audio_tag(meta)
        existing_tag = self._existing_audio_tag(name)

        if metadata_tag is None and existing_tag is None:
            return {"name": name}

        tag_kind = "dub" if "dub" in (metadata_tag, existing_tag) else "vie"
        cleaned_name = self._remove_existing_audio_tags(name)
        return {"name": self._insert_audio_tag(cleaned_name, tag_kind, meta)}

    @staticmethod
    def _resolve_tmdb_id(meta: Meta) -> int:
        for raw_id in (meta.tmdb, meta.tmdb_id):
            try:
                tmdb_id = int(raw_id or 0)
            except TypeError, ValueError:
                continue
            if tmdb_id > 0:
                return tmdb_id
        return 0

    async def get_tmdb(self, meta: Meta) -> dict[str, str]:
        return {"tmdb": str(self._resolve_tmdb_id(meta))}

    async def get_additional_data(self, meta: Meta) -> dict[str, str]:
        return {"mod_queue_opt_in": await self.get_flag(meta, "modq")}
