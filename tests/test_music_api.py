from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import importlib.util
import logging
import sys

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "music_api.py"
_SPEC = importlib.util.spec_from_file_location("maibot_music_api_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
MusicAPIResponseError = _MODULE.MusicAPIResponseError
MusicSearchClient = _MODULE.MusicSearchClient


class FakeResponse:
    def __init__(self, data: object) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._data


class InvalidJsonResponse(FakeResponse):
    def json(self) -> object:
        raise _MODULE.json.JSONDecodeError("invalid", "<html>", 0)


def make_client() -> MusicSearchClient:
    client = object.__new__(MusicSearchClient)
    client._netease_client = SimpleNamespace(get=AsyncMock())
    client._qq_client = SimpleNamespace(post=AsyncMock())
    client._qq_cookie = {}
    return client


@pytest.mark.asyncio
async def test_search_netease_parses_valid_response() -> None:
    client = make_client()
    client._netease_client.get.return_value = FakeResponse(
        {
            "code": 200,
            "result": {
                "songs": [
                    {
                        "id": 123,
                        "name": "测试歌曲",
                        "artists": [{"name": "测试歌手"}],
                        "album": {"name": "测试专辑"},
                    }
                ]
            },
        }
    )

    results = await client.search_netease("测试", limit=3)

    assert len(results) == 1
    assert results[0].song_id == "123"
    assert results[0].artists == "测试歌手"
    client._netease_client.get.assert_awaited_once_with(
        "https://music.163.com/api/search/get/web",
        params={"s": "测试", "type": "1", "limit": "3", "offset": "0"},
    )


@pytest.mark.asyncio
async def test_search_netease_accepts_valid_empty_result() -> None:
    client = make_client()
    client._netease_client.get.return_value = FakeResponse({"code": 200, "result": {}})

    assert await client.search_netease("不存在") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"code": 200, "result": "异常响应"}, "result_type=str result='异常响应'"),
        ({"code": 500, "result": {}}, "code=500"),
        ({"code": 200, "result": {"songs": "异常响应"}}, "songs 不是列表"),
    ],
)
async def test_search_netease_rejects_invalid_response(response: object, message: str) -> None:
    client = make_client()
    client._netease_client.get.return_value = FakeResponse(response)

    with pytest.raises(MusicAPIResponseError, match=message):
        await client.search_netease("everlasting liberty")


@pytest.mark.asyncio
async def test_search_netease_limits_string_result_in_error() -> None:
    client = make_client()
    result = "异常响应" * 200
    client._netease_client.get.return_value = FakeResponse({"code": 200, "result": result})

    with pytest.raises(MusicAPIResponseError) as exc_info:
        await client.search_netease("测试")

    message = str(exc_info.value)
    assert f"result={result[:512]!r}" in message
    assert f"result_length={len(result)}" in message
    assert result not in message


@pytest.mark.asyncio
async def test_search_netease_rejects_invalid_json() -> None:
    client = make_client()
    client._netease_client.get.return_value = InvalidJsonResponse(None)

    with pytest.raises(MusicAPIResponseError, match="不是有效 JSON"):
        await client.search_netease("everlasting liberty")


@pytest.mark.asyncio
async def test_search_qq_extracts_media_mid() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_1": {
                "code": 0,
                "data": {
                    "body": {
                        "song": {
                            "list": [
                                {
                                    "mid": "song-mid",
                                    "name": "测试歌曲",
                                    "singer": [{"name": "测试歌手"}],
                                    "album": {"name": "测试专辑"},
                                    "file": {"media_mid": "media-mid"},
                                }
                            ]
                        }
                    }
                },
            },
        }
    )

    results = await client.search_qq("测试")

    assert len(results) == 1
    assert results[0].song_id == "song-mid"
    assert results[0].media_id == "media-mid"


@pytest.mark.asyncio
async def test_search_qq_rejects_business_error() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {"code": 0, "req_1": {"code": 1000, "data": {}}}
    )

    with pytest.raises(MusicAPIResponseError, match="QQ音乐搜索业务失败"):
        await client.search_qq("测试")


@pytest.mark.asyncio
async def test_qq_song_detail_extracts_media_mid() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_0": {
                "code": 0,
                "data": {
                    "track_info": {
                        "mid": "song-mid",
                        "name": "测试歌曲",
                        "singer": [{"name": "测试歌手"}],
                        "album": {"name": "测试专辑"},
                        "file": {"media_mid": "different-media-mid"},
                    }
                },
            },
        }
    )

    detail = await client.get_qq_song_detail("song-mid")

    assert detail is not None
    assert detail.media_id == "different-media-mid"


@pytest.mark.asyncio
async def test_qq_song_detail_rejects_business_error() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {"code": 0, "req_0": {"code": 1000, "data": {}}}
    )

    with pytest.raises(MusicAPIResponseError, match="QQ音乐歌曲详情业务失败"):
        await client.get_qq_song_detail("song-mid")


@pytest.mark.asyncio
async def test_qq_song_detail_rejects_invalid_json() -> None:
    client = make_client()
    client._qq_client.post.return_value = InvalidJsonResponse(None)

    with pytest.raises(MusicAPIResponseError, match="歌曲详情响应不是有效 JSON"):
        await client.get_qq_song_detail("song-mid")


@pytest.mark.asyncio
async def test_qq_local_mode_requests_only_mp3_candidates() -> None:
    client = make_client()
    client._get_qq_vkey_batch = AsyncMock(return_value="https://example.test/song.mp3")

    await client._get_qq_song_url("song-mid", "media-mid", mp3_only=True)

    client._get_qq_vkey_batch.assert_awaited_once_with(
        [
            "M800media-mid.mp3",
            "M500media-mid.mp3",
        ],
        "song-mid",
    )


@pytest.mark.asyncio
async def test_qq_remote_mode_preserves_all_candidates() -> None:
    client = make_client()
    client._get_qq_vkey_batch = AsyncMock(return_value="https://example.test/song.flac")

    await client._get_qq_song_url("song-mid", "media-mid", mp3_only=False)

    client._get_qq_vkey_batch.assert_awaited_once_with(
        [
            "F000media-mid.flac",
            "M800media-mid.mp3",
            "M500media-mid.mp3",
            "C400media-mid.m4a",
        ],
        "song-mid",
    )


@pytest.mark.asyncio
async def test_qq_missing_media_mid_uses_song_mid_as_resource_id() -> None:
    client = make_client()
    client._get_qq_vkey_batch = AsyncMock(return_value=None)

    await client._get_qq_song_url("song-mid", mp3_only=True)

    client._get_qq_vkey_batch.assert_awaited_once_with(
        ["M800song-mid.mp3", "M500song-mid.mp3"],
        "song-mid",
    )


@pytest.mark.asyncio
async def test_qq_vkey_uses_anonymous_login_flag_and_falls_back_by_quality() -> None:
    client = make_client()
    filenames = ["M800media-mid.mp3", "M500media-mid.mp3"]
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_0": {
                "code": 0,
                "data": {
                    "sip": ["https://isure.stream.qqmusic.qq.com/"],
                    "midurlinfo": [
                        {"filename": filenames[0], "purl": "", "result": 0},
                        {"filename": filenames[1], "purl": "audio/test.mp3", "result": 0},
                    ],
                },
            },
        }
    )

    audio_url = await client._get_qq_vkey_batch(filenames, "song-mid")

    assert audio_url == "https://isure.stream.qqmusic.qq.com/audio/test.mp3"
    request_data = client._qq_client.post.await_args.kwargs["json"]
    assert request_data["req_0"]["param"]["loginflag"] == 1
    assert request_data["req_0"]["param"]["uin"] == "0"


@pytest.mark.asyncio
async def test_qq_vkey_skips_invalid_url_and_uses_next_quality() -> None:
    client = make_client()
    filenames = ["M800media-mid.mp3", "M500media-mid.mp3"]
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_0": {
                "code": 0,
                "data": {
                    "sip": [],
                    "midurlinfo": [
                        {"filename": filenames[0], "purl": "not-a-valid-url", "result": 0},
                        {
                            "filename": filenames[1],
                            "purl": "https://example.test/audio.mp3",
                            "result": 0,
                        },
                    ],
                },
            },
        }
    )

    audio_url = await client._get_qq_vkey_batch(filenames, "song-mid")

    assert audio_url == "https://example.test/audio.mp3"


@pytest.mark.asyncio
async def test_qq_vkey_rejects_business_error() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {"code": 0, "req_0": {"code": 1000, "data": {}}}
    )

    assert await client._get_qq_vkey_batch(["M500media-mid.mp3"], "song-mid") is None


@pytest.mark.asyncio
async def test_qq_vkey_logs_empty_candidates(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_0": {
                "code": 0,
                "data": {
                    "sip": ["https://isure.stream.qqmusic.qq.com/"],
                    "midurlinfo": [
                        {
                            "filename": "M500media-mid.mp3",
                            "purl": "",
                            "result": -1,
                            "subcode": 1001,
                        }
                    ],
                },
            },
        }
    )

    with caplog.at_level(logging.WARNING, logger="maibot-music.api"):
        result = await client._get_qq_vkey_batch(["M500media-mid.mp3"], "song-mid")

    assert result is None
    assert "QQ音乐vkey未返回可用音频" in caplog.text
    assert "purl_present" in caplog.text
