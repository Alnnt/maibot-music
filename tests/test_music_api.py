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
    def __init__(self, data: object, text: str = "") -> None:
        self._data = data
        self.text = text

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
    client._qq_cookie = {
        "uin": "1234567890",
        "qqmusic_key": "configured-key",
    }
    return client


@pytest.mark.asyncio
async def test_qq_client_uses_configured_login_cookie() -> None:
    client = MusicSearchClient(
        qq_cookie={
            "uin": " 1234567890 ",
            "qqmusic_key": " configured-key ",
        }
    )

    try:
        request = client._qq_client.build_request(
            "POST",
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
        )

        assert "uin=1234567890" in request.headers["cookie"]
        assert "qqmusic_key=configured-key" in request.headers["cookie"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qq_cookie", "expected_missing"),
    [
        ({}, "qq.uin, qq.qqmusic_key"),
        ({"uin": "1234567890"}, "qq.qqmusic_key"),
        ({"qqmusic_key": "configured-key"}, "qq.uin"),
        ({"uin": " ", "qqmusic_key": "\t"}, "qq.uin, qq.qqmusic_key"),
    ],
)
async def test_search_qq_requires_complete_login_credentials(
    qq_cookie: dict[str, str],
    expected_missing: str,
) -> None:
    client = make_client()
    client._qq_cookie = qq_cookie

    with pytest.raises(MusicAPIResponseError) as exc_info:
        await client.search_qq("测试")

    assert str(exc_info.value) == f"QQ音乐搜索需要登录，缺少配置: {expected_missing}"
    client._qq_client.post.assert_not_awaited()


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
async def test_netease_eapi_reuses_search_session_cookie() -> None:
    captured_requests: list[_MODULE.httpx.Request] = []

    def handler(request: _MODULE.httpx.Request) -> _MODULE.httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/api/search/get/web":
            return _MODULE.httpx.Response(
                200,
                headers={"Set-Cookie": "NMTID=session-id; Domain=.music.163.com; Path=/"},
                json={"code": 200, "result": {}},
                request=request,
            )
        if request.url.path == "/redirect":
            return _MODULE.httpx.Response(
                302,
                headers={"Location": "https://example.test/song.mp3"},
                request=request,
            )
        if request.url.host == "example.test":
            return _MODULE.httpx.Response(200, request=request)
        return _MODULE.httpx.Response(
            200,
            json={
                "code": 200,
                "data": [{"url": "https://example.test/song.mp3"}],
            },
            request=request,
        )

    client = MusicSearchClient(
        netease_cookie={
            "MUSIC_U": "configured-music-u",
            "__csrf": "configured-csrf",
        }
    )
    netease_cookies = client._netease_client.cookies
    await client._netease_client.aclose()
    client._netease_client = _MODULE.httpx.AsyncClient(
        headers=_MODULE._NETEASE_HEADERS,
        cookies=netease_cookies,
        transport=_MODULE.httpx.MockTransport(handler),
        timeout=_MODULE._REQUEST_TIMEOUT,
        follow_redirects=True,
    )

    try:
        await client.search_netease("测试", limit=1)
        audio_url = await client._get_netease_eapi_url("123", 320000)

        assert audio_url == "https://example.test/song.mp3"
        assert len(captured_requests) == 2
        eapi_request = captured_requests[1]
        assert eapi_request.url.host == "interface.music.163.com"
        assert "NMTID=session-id" in eapi_request.headers["cookie"]
        assert "MUSIC_U=configured-music-u" in eapi_request.headers["cookie"]
        assert "__csrf=configured-csrf" in eapi_request.headers["cookie"]
        assert eapi_request.headers["user-agent"] == _MODULE._EAPI_HEADERS["User-Agent"]
        assert eapi_request.headers["referer"] == _MODULE._EAPI_HEADERS["Referer"]

        external_request = client._netease_client.build_request(
            "GET",
            "https://example.test/song.mp3",
        )
        assert "cookie" not in external_request.headers

        redirect_response = await client._netease_client.get(
            "https://music.163.com/redirect",
        )
        assert redirect_response.status_code == 200
        assert "MUSIC_U=configured-music-u" in captured_requests[2].headers["cookie"]
        assert "__csrf=configured-csrf" in captured_requests[2].headers["cookie"]
        assert "cookie" not in captured_requests[3].headers
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_netease_eapi_refreshes_url_after_cold_session_initialization() -> None:
    captured_requests: list[_MODULE.httpx.Request] = []

    def handler(request: _MODULE.httpx.Request) -> _MODULE.httpx.Response:
        captured_requests.append(request)
        response_url = (
            "https://example.test/cold.mp3"
            if len(captured_requests) == 1
            else "https://example.test/initialized.mp3"
        )
        headers = {}
        if len(captured_requests) == 1:
            headers["Set-Cookie"] = "NMTID=session-id; Domain=.music.163.com; Path=/"
        return _MODULE.httpx.Response(
            200,
            headers=headers,
            json={"code": 200, "data": [{"url": response_url}]},
            request=request,
        )

    client = MusicSearchClient()
    await client._netease_client.aclose()
    client._netease_client = _MODULE.httpx.AsyncClient(
        headers=_MODULE._NETEASE_HEADERS,
        transport=_MODULE.httpx.MockTransport(handler),
        timeout=_MODULE._REQUEST_TIMEOUT,
        follow_redirects=True,
    )

    try:
        audio_url = await client._get_netease_eapi_url("123", 320000)

        assert audio_url == "https://example.test/initialized.mp3"
        assert len(captured_requests) == 2
        assert "cookie" not in captured_requests[0].headers
        assert "NMTID=session-id" in captured_requests[1].headers["cookie"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_netease_eapi_does_not_follow_redirects() -> None:
    captured_requests: list[_MODULE.httpx.Request] = []

    def handler(request: _MODULE.httpx.Request) -> _MODULE.httpx.Response:
        captured_requests.append(request)
        if request.url.host == "interface.music.163.com":
            return _MODULE.httpx.Response(
                302,
                headers={"Location": "https://example.test/redirected"},
                request=request,
            )
        return _MODULE.httpx.Response(200, request=request)

    client = MusicSearchClient()
    await client._netease_client.aclose()
    client._netease_client = _MODULE.httpx.AsyncClient(
        headers=_MODULE._NETEASE_HEADERS,
        transport=_MODULE.httpx.MockTransport(handler),
        timeout=_MODULE._REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    client._netease_client.cookies.set(
        "NMTID",
        "session-id",
        domain=".music.163.com",
        path="/",
    )

    try:
        audio_url = await client._get_netease_eapi_url("123", 320000)

        assert audio_url is None
        assert len(captured_requests) == 1
        assert captured_requests[0].url.host == "interface.music.163.com"
    finally:
        await client.close()


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
async def test_search_netease_preserves_full_string_result_in_diagnostic() -> None:
    client = make_client()
    result = "异常响应" * 3000
    client._netease_client.get.return_value = FakeResponse({"code": 200, "result": result})

    with pytest.raises(MusicAPIResponseError) as exc_info:
        await client.search_netease("测试")

    message = str(exc_info.value)
    assert f"result={result[:512]!r}" in message
    assert f"result_length={len(result)}" in message
    assert result not in message

    diagnostic = exc_info.value.diagnostic_message()
    assert result in diagnostic
    assert "truncated original_length" not in diagnostic


@pytest.mark.asyncio
async def test_search_netease_rejects_invalid_json() -> None:
    client = make_client()
    client._netease_client.get.return_value = InvalidJsonResponse(None)

    with pytest.raises(MusicAPIResponseError, match="不是有效 JSON"):
        await client.search_netease("everlasting liberty")


@pytest.mark.asyncio
async def test_search_netease_timeout_preserves_reason() -> None:
    client = make_client()
    client._netease_client.get.side_effect = _MODULE.httpx.ReadTimeout("request timed out")

    with pytest.raises(MusicAPIResponseError, match="网易云音乐搜索超时") as exc_info:
        await client.search_netease("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert "ReadTimeout" in diagnostic
    assert "request timed out" in diagnostic


@pytest.mark.asyncio
async def test_search_netease_http_error_preserves_full_response() -> None:
    client = make_client()
    body = "上游拦截响应" * 3000
    request = _MODULE.httpx.Request("GET", "https://music.163.com/api/search/get/web")
    client._netease_client.get.return_value = _MODULE.httpx.Response(
        403,
        request=request,
        text=body,
    )

    with pytest.raises(MusicAPIResponseError, match="status=403") as exc_info:
        await client.search_netease("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert body in diagnostic
    assert "truncated original_length" not in diagnostic


@pytest.mark.asyncio
async def test_search_netease_network_error_preserves_reason() -> None:
    client = make_client()
    client._netease_client.get.side_effect = _MODULE.httpx.ConnectError("connection refused")

    with pytest.raises(MusicAPIResponseError, match="网易云音乐搜索网络异常") as exc_info:
        await client.search_netease("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert "ConnectError" in diagnostic
    assert "connection refused" in diagnostic


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
    client._qq_client.post.assert_awaited_once()
    req_data = client._qq_client.post.await_args.kwargs["json"]
    assert req_data["loginUin"] == "1234567890"
    assert req_data["comm"]["uin"] == "1234567890"
    assert req_data["comm"]["authst"] == "configured-key"


@pytest.mark.asyncio
async def test_search_qq_rejects_business_error() -> None:
    client = make_client()
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "traceid": "trace-123",
            "req_1": {
                "code": 2001,
                "data": {
                    "area": "sz",
                    "feedbackURL": "https://y.qq.com/wk_v17/common_login.html#/login?QQAppid=100497308",
                    "reason": "需要登录",
                    "authst": "secret-authst",
                    "MUSIC_A": "secret-music-a",
                    "MUSIC_U": "secret-music-u",
                    "token": "secret-token",
                },
            },
        }
    )

    with pytest.raises(
        MusicAPIResponseError,
        match="登录态失效或被拒绝.*module_code=2001",
    ) as exc_info:
        await client.search_qq("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert '"traceid":"trace-123"' in diagnostic
    assert '"area":"sz"' in diagnostic
    assert '"feedbackURL":"https://y.qq.com/wk_v17/common_login.html#/login?QQAppid=100497308"' in diagnostic
    assert '"reason":"需要登录"' in diagnostic
    assert diagnostic.count("[REDACTED]") == 4
    assert "secret-authst" not in diagnostic
    assert "secret-music-a" not in diagnostic
    assert "secret-music-u" not in diagnostic
    assert "secret-token" not in diagnostic


@pytest.mark.asyncio
async def test_search_qq_preserves_full_large_error_response() -> None:
    client = make_client()
    reason = "完整上游错误内容" * 2000
    client._qq_client.post.return_value = FakeResponse(
        {
            "code": 0,
            "req_1": {
                "code": 2001,
                "data": {"reason": reason},
            },
        }
    )

    with pytest.raises(MusicAPIResponseError) as exc_info:
        await client.search_qq("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert reason in diagnostic
    assert "truncated original_length" not in diagnostic


@pytest.mark.asyncio
async def test_search_qq_invalid_json_redacts_text_response() -> None:
    client = make_client()
    client._qq_client.post.return_value = InvalidJsonResponse(
        None,
        (
            "upstream failed token='space separated secret'; "
            "Authorization: Bearer secret-authorization\n"
            "Cookie: session=secret-session; MUSIC_U=secret-music-u\n"
            'callback({"credential":{"primary":"secret-primary"},'
            '"ticket":["secret-a","secret-b"],'
            '"secret":"escaped \\"secret-value\\" text"});\n'
            "reason=访问过于频繁; "
            "qqmusic_key=secret-qqmusic-key; "
            "qm_keyst=secret-qm-keyst; "
            "p_skey=secret-p-skey; "
            "skey=secret-skey; "
            "MUSIC_A=secret-music-a"
        ),
    )

    with pytest.raises(MusicAPIResponseError, match="不是有效 JSON") as exc_info:
        await client.search_qq("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert "upstream failed" in diagnostic
    assert "reason=访问过于频繁" in diagnostic
    assert diagnostic.count("[REDACTED]") == 11
    assert "space separated secret" not in diagnostic
    assert "secret-authorization" not in diagnostic
    assert "secret-session" not in diagnostic
    assert "secret-music-u" not in diagnostic
    assert "secret-primary" not in diagnostic
    assert "secret-a" not in diagnostic
    assert "secret-b" not in diagnostic
    assert "secret-value" not in diagnostic
    assert "secret-qqmusic-key" not in diagnostic
    assert "secret-qm-keyst" not in diagnostic
    assert "secret-p-skey" not in diagnostic
    assert "secret-skey" not in diagnostic
    assert "secret-music-a" not in diagnostic


@pytest.mark.asyncio
async def test_search_qq_network_error_preserves_reason() -> None:
    client = make_client()
    client._qq_client.post.side_effect = _MODULE.httpx.ConnectError("connection refused")

    with pytest.raises(MusicAPIResponseError, match="QQ音乐搜索网络异常") as exc_info:
        await client.search_qq("测试")

    diagnostic = exc_info.value.diagnostic_message()
    assert "ConnectError" in diagnostic
    assert "connection refused" in diagnostic


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
async def test_qq_vkey_uses_login_credentials_and_falls_back_by_quality() -> None:
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
    assert request_data["req_0"]["param"]["uin"] == "1234567890"
    assert request_data["loginUin"] == "1234567890"
    assert request_data["comm"]["uin"] == "1234567890"
    assert request_data["comm"]["authst"] == "configured-key"


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
