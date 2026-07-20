from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

_MODULE_PATH = Path(__file__).parents[1] / "audio_cache.py"
_SPEC = importlib.util.spec_from_file_location("maibot_music_audio_cache", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
AudioCacheError = _MODULE.AudioCacheError
MusicAudioCache = _MODULE.MusicAudioCache

_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
_MP3_DATA = _MP3_FRAME * 2


def _build_cache(tmp_path: Path, handler: httpx.MockTransport, **kwargs: int) -> MusicAudioCache:
    client = httpx.AsyncClient(transport=handler)
    return MusicAudioCache(
        str(tmp_path),
        "/app/music_cache",
        max_size_bytes=kwargs.get("max_size_bytes", 1024),
        expire_seconds=kwargs.get("expire_seconds", 3600),
        max_file_size_bytes=kwargs.get("max_file_size_bytes", 1024),
        download_timeout_seconds=10,
        client=client,
    )


@pytest.mark.asyncio
async def test_downloads_mp3_and_reuses_cache(tmp_path: Path) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg"},
            content=_MP3_DATA,
            request=request,
        )

    cache = _build_cache(tmp_path, httpx.MockTransport(handler))
    await cache.initialize()

    first_path = await cache.get_or_download("163", "123", "https://example.test/song.mp3")
    await cache.release(first_path)
    first_mtime = first_path.stat().st_mtime
    await cache.get_or_download("163", "123", "https://example.test/song.mp3")

    assert request_count == 1
    assert first_path.read_bytes() == _MP3_DATA
    assert first_path.stat().st_mtime >= first_mtime
    assert cache.napcat_path(first_path) == "/app/music_cache/163_123.mp3"

    await cache.release(first_path)
    await cache.close()


@pytest.mark.asyncio
async def test_rejects_non_mp3_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/flac"},
            content=b"fLaC" + b"\x00" * 64,
            request=request,
        )

    cache = _build_cache(tmp_path, httpx.MockTransport(handler))
    await cache.initialize()

    with pytest.raises(AudioCacheError, match="不是 MP3"):
        await cache.get_or_download("qq", "abc", "https://example.test/song.flac")

    assert list(tmp_path.iterdir()) == []
    await cache.close()


@pytest.mark.asyncio
async def test_rejects_oversized_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg"},
            content=_MP3_DATA,
            request=request,
        )

    cache = _build_cache(
        tmp_path,
        httpx.MockTransport(handler),
        max_file_size_bytes=16,
    )
    await cache.initialize()

    with pytest.raises(AudioCacheError, match="大小限制"):
        await cache.get_or_download("163", "123", "https://example.test/song.mp3")

    assert list(tmp_path.iterdir()) == []
    await cache.close()


@pytest.mark.asyncio
async def test_cleanup_removes_expired_and_keeps_active_file(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_MP3_DATA, request=request)
    )
    cache = _build_cache(tmp_path, transport, expire_seconds=60)
    await cache.initialize()

    expired_path = tmp_path / "163_expired.mp3"
    active_path = tmp_path / "163_active.mp3"
    expired_path.write_bytes(_MP3_DATA)
    active_path.write_bytes(_MP3_DATA)
    old_time = time.time() - 120
    os.utime(expired_path, (old_time, old_time))
    os.utime(active_path, (old_time, old_time))
    cache._active_paths[active_path.resolve()] = 1

    await cache.cleanup()

    assert not expired_path.exists()
    assert active_path.exists()

    await cache.release(active_path)
    await cache.close()


@pytest.mark.asyncio
async def test_size_limit_evicts_least_recently_used_file(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_MP3_DATA, request=request)
    )
    cache = _build_cache(tmp_path, transport, max_size_bytes=len(_MP3_DATA) * 2)
    await cache.initialize()

    oldest = tmp_path / "163_oldest.mp3"
    middle = tmp_path / "163_middle.mp3"
    newest = tmp_path / "163_newest.mp3"
    for path in (oldest, middle, newest):
        path.write_bytes(_MP3_DATA)
    now = time.time()
    os.utime(oldest, (now - 30, now - 30))
    os.utime(middle, (now - 20, now - 20))
    os.utime(newest, (now - 10, now - 10))

    await cache.enforce_size_limit()

    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()
    await cache.close()


@pytest.mark.asyncio
async def test_concurrent_references_keep_file_protected(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=_MP3_DATA, request=request)
    )
    cache = _build_cache(tmp_path, transport, expire_seconds=1)
    await cache.initialize()

    first_path = await cache.get_or_download("163", "123", "https://example.test/song.mp3")
    second_path = await cache.get_or_download("163", "123", "https://example.test/song.mp3")
    old_time = time.time() - 10
    os.utime(first_path, (old_time, old_time))

    await cache.release(first_path)
    await cache.cleanup()
    assert second_path.exists()

    await cache.release(second_path)
    await cache.cleanup()
    assert not second_path.exists()
    await cache.close()


@pytest.mark.asyncio
async def test_symlink_is_not_used_as_cache_file(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.mp3"
    outside_path.write_bytes(_MP3_DATA)
    symlink_path = tmp_path / "163_123.mp3"
    try:
        symlink_path.symlink_to(outside_path)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, content=_MP3_DATA, request=request)

    cache = _build_cache(tmp_path, httpx.MockTransport(handler))
    await cache.initialize()
    cache_path = await cache.get_or_download("163", "123", "https://example.test/song.mp3")

    assert request_count == 1
    assert not cache_path.is_symlink()
    assert outside_path.read_bytes() == _MP3_DATA

    await cache.release(cache_path)
    await cache.close()
    outside_path.unlink()


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started
        self.wait_forever = asyncio.Event()

    async def __aiter__(self):
        yield _MP3_FRAME
        self.started.set()
        await self.wait_forever.wait()


@pytest.mark.asyncio
async def test_cancelled_download_cleans_state_and_temp_file(tmp_path: Path) -> None:
    started = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BlockingStream(started), request=request)

    cache = _build_cache(tmp_path, httpx.MockTransport(handler))
    await cache.initialize()
    task = asyncio.create_task(
        cache.get_or_download("163", "123", "https://example.test/song.mp3")
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cache._active_paths == {}
    assert cache._download_states == {}
    assert list(tmp_path.iterdir()) == []
    await cache.close()
