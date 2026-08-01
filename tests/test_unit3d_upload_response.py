from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    import data.config  # noqa: F401
except ImportError:
    from data import example_config

    sys.modules["data.config"] = example_config

unit3d_module = importlib.import_module("src.trackers.UNIT3D")
UNIT3D = unit3d_module.UNIT3D


class _ResponseTracker(UNIT3D):
    base_url = "https://tracker.example"
    upload_url = f"{base_url}/api/torrents/upload"


def _tracker() -> _ResponseTracker:
    return _ResponseTracker(
        {"DEFAULT": {}, "TRACKERS": {"TEST": {"api_key": "synthetic-api-key"}}},
        "TEST",
    )


@pytest.mark.parametrize(
    ("response_data", "expected"),
    [
        (
            {"data": "https://tracker.example/torrent/download/374352.382"},
            "https://tracker.example/torrent/download/374352.382",
        ),
        (
            {"data": "/torrent/download/374352.382"},
            "https://tracker.example/torrent/download/374352.382",
        ),
        (
            {"data": "torrent/download/374352.382"},
            "https://tracker.example/torrent/download/374352.382",
        ),
        (
            {"data": " 374352 "},
            "https://tracker.example/torrent/download/374352",
        ),
        (
            {"data": 374352},
            "https://tracker.example/torrent/download/374352",
        ),
    ],
)
def test_resolve_upload_download_url(response_data, expected):
    assert _tracker().resolve_upload_download_url(response_data) == expected


@pytest.mark.parametrize(
    "response_data",
    [
        {"data": "https://other.example/torrent/download/374352"},
        {"data": "//other.example/torrent/download/374352"},
        {"data": "http://tracker.example/torrent/download/374352"},
        {"data": "https://tracker.example:444/torrent/download/374352"},
        {"data": "https://tracker.example@other.example/torrent/download/374352"},
        {"data": "http://[bad"},
        {"data": "//[bad"},
        {"data": r"https:\\other.example\torrent\download\374352"},
        {"data": True},
        {"data": None},
        {"data": {"id": 374352}},
    ],
)
def test_resolve_upload_download_url_rejects_unsafe_or_invalid_data(response_data):
    assert _tracker().resolve_upload_download_url(response_data) is None


@pytest.mark.parametrize(
    ("response_data", "expected"),
    [
        ({"data": 374352}, "374352"),
        ({"data": "374352"}, "374352"),
        ({"data": "/torrent/download/374352"}, "374352"),
        ({"data": "https://tracker.example/torrent/download/374352.382"}, "374352"),
        ({"data": "https://other.example/torrent/download/374352.382"}, ""),
        ({"data": "/torrent/download/not-an-id"}, ""),
    ],
)
def test_get_torrent_id_supports_numeric_and_safe_url_data(response_data, expected):
    assert asyncio.run(_tracker().get_torrent_id(response_data)) == expected


class _UploadResponse:
    def __init__(self, response_data):
        self._response_data = response_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._response_data


def _client_for(response_data, post_calls):
    class _UploadClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def post(self, **_kwargs):
            post_calls.append(1)
            return _UploadResponse(response_data)

    return _UploadClient


def _upload_meta(tmp_path):
    uuid = "unit3d-response"
    torrent_dir = tmp_path / "tmp" / uuid
    torrent_dir.mkdir(parents=True)
    (torrent_dir / "BASE.torrent").write_bytes(b"torrent fixture")
    return SimpleNamespace(
        base_dir=str(tmp_path),
        uuid=uuid,
        ua_name="Upload Assistant",
        current_version="test",
        debug=False,
        tracker_status={"TEST": {}},
    )


def _prepare_upload(tracker, download_mock):
    tracker.get_data = AsyncMock(return_value={})
    tracker.get_additional_files = AsyncMock(return_value={})
    tracker.common.get_torrent_filename = AsyncMock(return_value="BASE")
    tracker.common.download_tracker_torrent = download_mock


def test_upload_resolves_numeric_id_before_downloading(monkeypatch, tmp_path):
    tracker = _tracker()
    download_mock = AsyncMock(return_value=None)
    _prepare_upload(tracker, download_mock)
    post_calls = []
    monkeypatch.setattr(unit3d_module.httpx, "AsyncClient", _client_for({"success": True, "data": 374352}, post_calls))

    result = asyncio.run(tracker.upload(_upload_meta(tmp_path)))

    assert result is True
    assert len(post_calls) == 1
    assert tracker.common.download_tracker_torrent.await_args.kwargs["downurl"] == "https://tracker.example/torrent/download/374352"


def test_upload_does_not_fetch_off_origin_response_url(monkeypatch, tmp_path):
    tracker = _tracker()
    download_mock = AsyncMock(return_value=None)
    _prepare_upload(tracker, download_mock)
    post_calls = []
    monkeypatch.setattr(
        unit3d_module.httpx,
        "AsyncClient",
        _client_for({"success": True, "data": "https://other.example/torrent/download/374352"}, post_calls),
    )

    result = asyncio.run(tracker.upload(_upload_meta(tmp_path)))

    assert result is True
    assert len(post_calls) == 1
    download_mock.assert_not_awaited()


def test_download_failure_does_not_repeat_successful_upload_post(monkeypatch, tmp_path):
    tracker = _tracker()
    download_mock = AsyncMock(side_effect=RuntimeError("synthetic download failure"))
    _prepare_upload(tracker, download_mock)
    post_calls = []
    monkeypatch.setattr(
        unit3d_module.httpx,
        "AsyncClient",
        _client_for({"success": True, "data": "/torrent/download/374352.382"}, post_calls),
    )

    result = asyncio.run(tracker.upload(_upload_meta(tmp_path)))

    assert result is True
    assert len(post_calls) == 1
    download_mock.assert_awaited_once()
