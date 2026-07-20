from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

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
SongInfo = music_api.SongInfo


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
