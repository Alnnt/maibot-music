from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

_PLUGIN_ROOT = Path(__file__).parents[1]
_PACKAGE_NAME = "maibot_music_test_package"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_PLUGIN_ROOT)]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)


def _load_module(module_name: str, file_name: str) -> types.ModuleType:
    full_name = f"{_PACKAGE_NAME}.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, _PLUGIN_ROOT / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("audio_cache", "audio_cache.py")
music_api = _load_module("music_api", "music_api.py")
_load_module("url_parser", "url_parser.py")
plugin_module = _load_module("plugin", "plugin.py")
MusicPlugin = plugin_module.MusicPlugin
MusicAPIResponseError = music_api.MusicAPIResponseError
SongInfo = music_api.SongInfo


class FakeResponse:
    def __init__(self, data: object) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._data


class FakeAudioCache:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.released_path: Path | None = None

    async def get_or_download(self, platform: str, song_id: str, audio_url: str) -> Path:
        assert platform == "163"
        assert song_id == "123"
        assert audio_url == "https://example.test/song.mp3"
        return self.cache_path

    def napcat_path(self, cache_path: Path) -> str:
        assert cache_path == self.cache_path
        return f"/app/music_cache/{cache_path.name}"

    async def release(self, cache_path: Path) -> None:
        self.released_path = cache_path


@pytest.mark.asyncio
async def test_search_protocol_error_logs_reason_without_traceback() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(default_platform="163", search_limit=5),
    )
    logger = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(text=AsyncMock()),
        logger=logger,
    )
    api = types.SimpleNamespace(
        search=AsyncMock(
            side_effect=MusicAPIResponseError(
                "网易云音乐搜索响应格式错误: query='廉价 洛天依' "
                "result_type=str result='访问过于频繁'"
            )
        ),
    )
    plugin._get_api = lambda: api

    success, message = await plugin._do_search_and_send("廉价 洛天依", stream_id="stream-1")

    assert success is False
    assert message == "搜索歌曲时出错，请稍后再试"
    logger.error.assert_called_once_with(
        "音乐搜索失败: %s",
        api.search.side_effect,
    )
    logger.exception.assert_not_called()
    plugin.ctx.send.text.assert_awaited_once_with("搜索歌曲时出错，请稍后再试", "stream-1")


@pytest.mark.asyncio
async def test_unexpected_search_error_keeps_traceback_logging() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(default_platform="163", search_limit=5),
    )
    logger = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(text=AsyncMock()),
        logger=logger,
    )
    api = types.SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("unexpected")))
    plugin._get_api = lambda: api

    success, _ = await plugin._do_search_and_send("测试", stream_id="stream-1")

    assert success is False
    logger.error.assert_not_called()
    logger.exception.assert_called_once_with("音乐搜索异常: %s", "测试")


@pytest.mark.asyncio
async def test_tool_search_protocol_errors_log_reasons_without_traceback() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(default_platform="163", search_limit=5),
    )
    logger = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._ctx = types.SimpleNamespace(logger=logger)
    api = types.SimpleNamespace(
        search=AsyncMock(
            side_effect=[
                MusicAPIResponseError("网易云音乐搜索响应格式错误: result_type=str result='访问过于频繁'"),
                MusicAPIResponseError("QQ音乐搜索业务失败: code=0 module_code=2001"),
            ]
        ),
    )
    plugin._get_api = lambda: api

    result = await plugin.handle_search_music(query="廉价 洛天依", stream_id="stream-1")

    assert result["name"] == "search_and_play_music"
    assert logger.error.call_count == 2
    assert "result='访问过于频繁'" in str(logger.error.call_args_list[0].args[2])
    assert "module_code=2001" in str(logger.error.call_args_list[1].args[2])
    logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_music_card_search_protocol_error_logs_reason_without_traceback() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(
            auto_parse_card=True,
            auto_parse_url=False,
            default_platform="163",
        ),
    )
    logger = types.SimpleNamespace(error=Mock(), exception=Mock(), info=Mock())
    plugin._ctx = types.SimpleNamespace(logger=logger)
    api = types.SimpleNamespace(
        search=AsyncMock(
            side_effect=MusicAPIResponseError(
                "网易云音乐搜索响应格式错误: result_type=str result='访问过于频繁'"
            )
        ),
    )
    plugin._get_api = lambda: api

    result = await plugin.handle_music_url_parse(
        message={
            "processed_plain_text": "[网易云音乐] 廉价 - 洛天依",
            "session_id": "stream-1",
        }
    )

    assert result == {"action": "continue"}
    logger.error.assert_called_once_with("音乐卡片搜索失败: %s", api.search.side_effect)
    logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_send_voice_audio_uses_napcat_local_path(tmp_path: Path) -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._audio_cache = FakeAudioCache(tmp_path / "163_123.mp3")
    plugin._voice_send_condition = asyncio.Condition()
    plugin._active_voice_sends = 0
    plugin._stopping_audio_cache = False
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(voice_source="local"),
    )
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(custom=AsyncMock(return_value=True)),
        logger=types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
    )
    api = types.SimpleNamespace(
        get_song_url=AsyncMock(return_value="https://example.test/song.mp3"),
    )
    plugin._get_api = lambda: api
    song = SongInfo(
        song_id="123",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        platform="163",
    )

    sent = await plugin._send_voice_audio(song, "stream-1")

    assert sent is True
    plugin.ctx.send.custom.assert_awaited_once_with(
        "voiceurl",
        {"url": "/app/music_cache/163_123.mp3"},
        "stream-1",
    )
    api.get_song_url.assert_awaited_once_with(
        "123",
        "163",
        "",
        mp3_only=True,
    )
    assert plugin._audio_cache.released_path == tmp_path / "163_123.mp3"


@pytest.mark.asyncio
async def test_qq_voice_uses_media_mid_from_song_detail() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._audio_cache = None
    plugin._voice_send_condition = asyncio.Condition()
    plugin._active_voice_sends = 0
    plugin._stopping_audio_cache = False
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(voice_source="remote"),
    )
    logger = types.SimpleNamespace(info=lambda *args: None, warning=Mock())
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(custom=AsyncMock(return_value=False)),
        logger=logger,
    )
    detail = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        platform="qq",
        media_id="different-media-mid",
    )
    api = types.SimpleNamespace(
        get_qq_song_detail=AsyncMock(return_value=detail),
        get_song_url=AsyncMock(return_value="https://example.test/song.mp3?vkey=secret"),
    )
    plugin._get_api = lambda: api
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        platform="qq",
    )

    sent = await plugin._send_voice_audio(song, "stream-1", silent=True)

    assert sent is False
    api.get_qq_song_detail.assert_awaited_once_with("song-mid")
    api.get_song_url.assert_awaited_once_with(
        "song-mid",
        "qq",
        "different-media-mid",
        mp3_only=False,
    )
    warning_args = logger.warning.call_args.args
    assert "secret" not in " ".join(str(arg) for arg in warning_args)


@pytest.mark.asyncio
async def test_qq_voice_resolves_media_mid_through_vkey_request() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._audio_cache = None
    plugin._voice_send_condition = asyncio.Condition()
    plugin._active_voice_sends = 0
    plugin._stopping_audio_cache = False
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(voice_source="remote"),
    )
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(custom=AsyncMock(return_value=True)),
        logger=types.SimpleNamespace(info=Mock(), warning=Mock()),
    )
    api = object.__new__(music_api.MusicSearchClient)
    api._qq_cookie = {}
    api._qq_client = types.SimpleNamespace(
        post=AsyncMock(
            side_effect=[
                FakeResponse(
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
                ),
                FakeResponse(
                    {
                        "code": 0,
                        "req_0": {
                            "code": 0,
                            "data": {
                                "sip": ["https://example.test/"],
                                "midurlinfo": [
                                    {
                                        "filename": "F000different-media-mid.flac",
                                        "purl": "audio/test.flac",
                                        "result": 0,
                                    },
                                    {
                                        "filename": "M800different-media-mid.mp3",
                                        "purl": "",
                                        "result": 0,
                                    },
                                    {
                                        "filename": "M500different-media-mid.mp3",
                                        "purl": "",
                                        "result": 0,
                                    },
                                    {
                                        "filename": "C400different-media-mid.m4a",
                                        "purl": "",
                                        "result": 0,
                                    },
                                ],
                            },
                        },
                    }
                ),
            ]
        )
    )
    plugin._get_api = lambda: api
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        platform="qq",
    )

    sent = await plugin._send_voice_audio(song, "stream-1", silent=True)

    assert sent is True
    vkey_request = api._qq_client.post.await_args_list[1].kwargs["json"]
    assert vkey_request["req_0"]["param"]["filename"] == [
        "F000different-media-mid.flac",
        "M800different-media-mid.mp3",
        "M500different-media-mid.mp3",
        "C400different-media-mid.m4a",
    ]
    plugin.ctx.send.custom.assert_awaited_once_with(
        "voiceurl",
        {"url": "https://example.test/audio/test.flac"},
        "stream-1",
    )


@pytest.mark.asyncio
async def test_voice_send_uses_source_and_cache_snapshot() -> None:
    plugin = object.__new__(MusicPlugin)
    original_cache = object()
    plugin._audio_cache = original_cache
    plugin._voice_send_condition = asyncio.Condition()
    plugin._active_voice_sends = 0
    plugin._stopping_audio_cache = False
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(voice_source="remote"),
    )

    voice_source, audio_cache = await plugin._acquire_voice_send()
    plugin._plugin_config_instance.music.voice_source = "local"
    plugin._audio_cache = object()

    assert voice_source == "remote"
    assert audio_cache is original_cache

    await plugin._release_voice_send()
    assert plugin._active_voice_sends == 0


@pytest.mark.asyncio
async def test_voice_send_waits_until_cache_gate_opens() -> None:
    plugin = object.__new__(MusicPlugin)
    cache = object()
    plugin._audio_cache = cache
    plugin._voice_send_condition = asyncio.Condition()
    plugin._active_voice_sends = 0
    plugin._stopping_audio_cache = True
    plugin._plugin_config_instance = types.SimpleNamespace(
        music=types.SimpleNamespace(voice_source="local"),
    )

    acquire_task = asyncio.create_task(plugin._acquire_voice_send())
    await asyncio.sleep(0)
    assert not acquire_task.done()

    async with plugin._voice_send_condition:
        plugin._stopping_audio_cache = False
        plugin._voice_send_condition.notify_all()

    assert await acquire_task == ("local", cache)
    await plugin._release_voice_send()
