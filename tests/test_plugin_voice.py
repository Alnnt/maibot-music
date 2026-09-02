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
error_log_module = _load_module("error_log", "error_log.py")
music_api = _load_module("music_api", "music_api.py")
_load_module("url_parser", "url_parser.py")
plugin_module = _load_module("plugin", "plugin.py")
MusicPlugin = plugin_module.MusicPlugin
PluginErrorLog = error_log_module.PluginErrorLog
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
    error_log = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._error_log = error_log
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
    error_log.error.assert_called_once_with(
        "音乐搜索失败: %s",
        api.search.side_effect.diagnostic_message(),
    )
    error_log.exception.assert_not_called()
    plugin.ctx.send.text.assert_awaited_once_with("搜索歌曲时出错，请稍后再试", "stream-1")


@pytest.mark.asyncio
async def test_unexpected_search_error_keeps_traceback_logging() -> None:
    plugin = object.__new__(MusicPlugin)
    error_log = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._error_log = error_log
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
    error_log.error.assert_not_called()
    error_log.exception.assert_called_once_with(
        "音乐搜索异常: platform=%s query=%r error=%r",
        "163",
        "测试",
        api.search.side_effect,
    )


@pytest.mark.asyncio
async def test_tool_search_protocol_errors_log_reasons_without_traceback() -> None:
    plugin = object.__new__(MusicPlugin)
    error_log = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._error_log = error_log
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
    assert error_log.error.call_count == 2
    assert "result='访问过于频繁'" in str(error_log.error.call_args_list[0].args[2])
    assert "module_code=2001" in str(error_log.error.call_args_list[1].args[2])
    error_log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_music_card_search_protocol_error_logs_reason_without_traceback() -> None:
    plugin = object.__new__(MusicPlugin)
    error_log = types.SimpleNamespace(error=Mock(), exception=Mock())
    plugin._error_log = error_log
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
    error_log.error.assert_called_once_with(
        "音乐卡片搜索失败(%s): %s",
        "163",
        api.search.side_effect.diagnostic_message(),
    )
    error_log.exception.assert_not_called()


def test_plugin_error_log_only_creates_file_after_error(tmp_path: Path) -> None:
    error_log = PluginErrorLog(tmp_path / "log")

    assert not (tmp_path / "log" / "error.log").exists()

    error_log.error("QQ音乐搜索失败: %s", "访问过于频繁")
    error_log.error("QQ音乐响应包含非法字符: %s", "\ud800")
    error_log.close()

    content = (tmp_path / "log" / "error.log").read_text(encoding="utf-8")
    assert "[ERROR] QQ音乐搜索失败: 访问过于频繁" in content
    assert "[ERROR] QQ音乐响应包含非法字符: \\ud800" in content


def test_plugin_error_log_preserves_existing_history(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    first_log = PluginErrorLog(log_dir)
    first_log.error("第一次错误")
    first_log.close()

    second_log = PluginErrorLog(log_dir)
    second_log.error("第二次错误")
    second_log.close()

    content = (log_dir / "error.log").read_text(encoding="utf-8")
    assert "第一次错误" in content
    assert "第二次错误" in content
    assert list(log_dir.glob("error.log.*")) == []


@pytest.mark.asyncio
async def test_on_load_uses_plugin_data_dir_for_error_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._error_log = None
    plugin._start_audio_cache = AsyncMock()
    plugin._ctx = types.SimpleNamespace(
        paths=types.SimpleNamespace(data_dir=tmp_path),
        logger=types.SimpleNamespace(info=Mock()),
    )
    error_log = types.SimpleNamespace(close=Mock())
    error_log_factory = Mock(return_value=error_log)
    monkeypatch.setattr(plugin_module, "PluginErrorLog", error_log_factory)

    await plugin.on_load()

    error_log_factory.assert_called_once_with(tmp_path / "log")
    plugin._start_audio_cache.assert_awaited_once_with()
    assert plugin._error_log is error_log


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


def _make_card_plugin(api: object) -> object:
    plugin = object.__new__(MusicPlugin)
    plugin._qq_direct_targets = {}
    plugin._ctx = types.SimpleNamespace(
        send=types.SimpleNamespace(
            custom=AsyncMock(return_value=True),
            text=AsyncMock(),
        ),
        logger=types.SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock()),
    )
    plugin._get_api = lambda: api
    return plugin


_QQ_CARD_PAYLOAD = {
    "type": "custom",
    "url": "https://y.qq.com/n/ryqq/songDetail/song-mid",
    "title": "测试歌曲",
    "image": "https://y.qq.com/music/photo_new/T002R300x300M000album-mid.jpg?max_age=2592000",
    "content": "测试歌手",
    "audio": "https://example.test/song.m4a?vkey=secret",
}


@pytest.mark.asyncio
async def test_send_music_card_qq_uses_custom_card_payload() -> None:
    api = types.SimpleNamespace(qq_music_card=AsyncMock(return_value=dict(_QQ_CARD_PAYLOAD)))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    api.qq_music_card.assert_awaited_once_with(song)
    plugin.ctx.send.custom.assert_awaited_once_with(
        "music",
        dict(_QQ_CARD_PAYLOAD),
        "stream-1",
    )
    plugin.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_qq_music_card_without_audio_sends_jump_card_with_hint() -> None:
    payload = dict(_QQ_CARD_PAYLOAD)
    payload["audio"] = ""
    api = types.SimpleNamespace(qq_music_card=AsyncMock(return_value=payload))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    expected = {key: value for key, value in payload.items() if value}
    assert "audio" not in expected
    plugin.ctx.send.custom.assert_awaited_once_with("music", expected, "stream-1")
    plugin.ctx.send.text.assert_awaited_once_with(
        "该歌曲受版权或登录限制无法直接播放，已发送可点击跳转的音乐卡片",
        "stream-1",
    )


@pytest.mark.asyncio
async def test_send_qq_music_card_jump_card_silent_has_no_hint() -> None:
    payload = dict(_QQ_CARD_PAYLOAD)
    payload["audio"] = ""
    api = types.SimpleNamespace(qq_music_card=AsyncMock(return_value=payload))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1", silent=True)

    assert sent is True
    plugin.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_qq_music_card_payload_none_notifies_and_returns_false() -> None:
    api = types.SimpleNamespace(qq_music_card=AsyncMock(return_value=None))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is False
    plugin.ctx.send.custom.assert_not_awaited()
    plugin.ctx.send.text.assert_awaited_once_with(
        "「测试歌曲 - 测试歌手」的QQ音乐卡片发送失败",
        "stream-1",
    )


@pytest.mark.asyncio
async def test_send_qq_music_card_error_uses_logger_and_fallback() -> None:
    api = types.SimpleNamespace(qq_music_card=AsyncMock(side_effect=RuntimeError("boom")))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is False
    plugin.ctx.logger.exception.assert_called_once()
    plugin.ctx.send.custom.assert_not_awaited()
    plugin.ctx.send.text.assert_awaited_once_with(
        "「测试歌曲 - 测试歌手」的QQ音乐卡片发送失败",
        "stream-1",
    )


@pytest.mark.asyncio
async def test_send_qq_music_card_payload_none_silent_stays_quiet() -> None:
    api = types.SimpleNamespace(qq_music_card=AsyncMock(return_value=None))
    plugin = _make_card_plugin(api)
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1", silent=True)

    assert sent is False
    plugin.ctx.send.custom.assert_not_awaited()
    plugin.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_music_card_netease_keeps_platform_segment() -> None:
    plugin = _make_card_plugin(types.SimpleNamespace())
    song = SongInfo(
        song_id="123",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="163",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    plugin.ctx.send.custom.assert_awaited_once_with(
        "music",
        {"type": "163", "id": "123"},
        "stream-1",
    )
    plugin.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_qq_music_card_prefers_direct_napcat_send() -> None:
    api = types.SimpleNamespace(
        qq_music_card=AsyncMock(return_value=dict(_QQ_CARD_PAYLOAD)),
        napcat_send_message=AsyncMock(return_value=(True, {"status": "ok"})),
    )
    plugin = _make_card_plugin(api)
    plugin._qq_direct_targets = {"stream-1": {"group_id": "12345"}}
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    expected_cq = (
        "[CQ:music,type=custom,url=https://y.qq.com/n/ryqq/songDetail/song-mid,"
        "audio=https://example.test/song.m4a?vkey=secret,title=测试歌曲,"
        "image=https://y.qq.com/music/photo_new/T002R300x300M000album-mid.jpg?max_age=2592000,"
        "content=测试歌手]"
    )
    api.napcat_send_message.assert_awaited_once_with(expected_cq, group_id="12345")
    plugin.ctx.send.custom.assert_not_awaited()
    plugin.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_qq_music_card_direct_fail_falls_back_to_adapter() -> None:
    api = types.SimpleNamespace(
        qq_music_card=AsyncMock(return_value=dict(_QQ_CARD_PAYLOAD)),
        napcat_send_message=AsyncMock(return_value=(False, {"retcode": 100, "message": "x"})),
    )
    plugin = _make_card_plugin(api)
    plugin._qq_direct_targets = {"stream-1": {"user_id": "987654"}}
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    api.napcat_send_message.assert_awaited_once()
    plugin.ctx.send.custom.assert_awaited_once_with(
        "music",
        dict(_QQ_CARD_PAYLOAD),
        "stream-1",
    )


@pytest.mark.asyncio
async def test_send_qq_music_card_resolves_target_via_chat_streams() -> None:
    api = types.SimpleNamespace(
        qq_music_card=AsyncMock(return_value=dict(_QQ_CARD_PAYLOAD)),
        napcat_send_message=AsyncMock(return_value=(True, {"status": "ok"})),
    )
    plugin = _make_card_plugin(api)
    plugin._ctx.chat = types.SimpleNamespace(
        get_all_streams=AsyncMock(
            return_value=[
                {
                    "stream_id": "other-stream",
                    "platform": "qq",
                    "group_id": "",
                    "user_id": "1",
                },
                {
                    "stream_id": "stream-1",
                    "platform": "qq",
                    "group_id": "111222",
                    "user_id": "2",
                },
            ]
        )
    )
    song = SongInfo(
        song_id="song-mid",
        name="测试歌曲",
        artists="测试歌手",
        album="",
        platform="qq",
    )

    sent = await plugin._send_music_card(song, "stream-1")

    assert sent is True
    plugin._ctx.chat.get_all_streams.assert_awaited_once_with(platform="qq")
    api.napcat_send_message.assert_awaited_once()
    assert api.napcat_send_message.await_args.kwargs == {"group_id": "111222"}
    # 命中后写入缓存
    assert plugin._qq_direct_targets == {"stream-1": {"group_id": "111222"}}


@pytest.mark.asyncio
async def test_remember_qq_direct_target_from_group_message() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._qq_direct_targets = {}
    message = {
        "platform": "qq",
        "session_id": "stream-1",
        "message_info": {
            "group_info": {"group_id": "657794518", "group_name": "测试群"},
            "user_info": {"user_id": "10001", "user_nickname": "某人"},
        },
    }

    plugin._remember_qq_direct_target("stream-1", message)

    assert plugin._qq_direct_targets == {"stream-1": {"group_id": "657794518"}}


@pytest.mark.asyncio
async def test_remember_qq_direct_target_from_private_message() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._qq_direct_targets = {}
    message = {
        "platform": "qq",
        "session_id": "stream-2",
        "message_info": {
            "group_info": None,
            "user_info": {"user_id": "10002", "user_nickname": "某人"},
        },
    }

    plugin._remember_qq_direct_target("stream-2", message)

    assert plugin._qq_direct_targets == {"stream-2": {"user_id": "10002"}}


@pytest.mark.asyncio
async def test_remember_qq_direct_target_ignores_non_qq_platform() -> None:
    plugin = object.__new__(MusicPlugin)
    plugin._qq_direct_targets = {}
    message = {
        "platform": "webui",
        "session_id": "stream-3",
        "message_info": {
            "group_info": {"group_id": "1"},
            "user_info": {"user_id": "1"},
        },
    }

    plugin._remember_qq_direct_target("stream-3", message)

    assert plugin._qq_direct_targets == {}


def test_build_qq_music_card_cq_escapes_display_text() -> None:
    payload = {
        "type": "custom",
        "url": "https://y.qq.com/n/ryqq/songDetail/song-mid",
        "title": "测试,歌曲[特别版]&Live",
        "image": "https://example.test/cover.jpg?x=1",
        "content": "歌手A,歌手B",
        "audio": "",
    }

    cq = plugin_module._build_qq_music_card_cq(payload)

    assert cq == (
        "[CQ:music,type=custom,url=https://y.qq.com/n/ryqq/songDetail/song-mid,"
        "title=测试&#44;歌曲&#91;特别版&#93;&amp;Live,"
        "image=https://example.test/cover.jpg?x=1,"
        "content=歌手A&#44;歌手B]"
    )
    assert "audio=" not in cq


