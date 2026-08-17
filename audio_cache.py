"""音乐音频本地缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

_MP3_CONTENT_TYPES = frozenset({
    "audio/mpeg",
    "audio/mp3",
    "audio/x-mpeg",
    "application/octet-stream",
})
_AUDIO_DOWNLOAD_HEADERS = {
    "163": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://music.163.com/",
    },
    "qq": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://y.qq.com/",
    },
}
_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_MPEG1_LAYER3_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MPEG2_LAYER3_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000)


class AudioCacheError(Exception):
    """音频缓存操作失败。"""


@dataclass(frozen=True)
class _MpegFrameInfo:
    """MPEG Layer III 帧头信息。"""

    length: int
    version_id: int
    sample_rate_index: int


@dataclass
class _DownloadState:
    """同一缓存键的下载协调状态。"""

    lock: asyncio.Lock
    references: int = 0


class MusicAudioCache:
    """下载并维护供 NapCat 读取的 MP3 音频缓存。"""

    def __init__(
        self,
        storage_dir: str,
        napcat_dir: str,
        *,
        max_size_bytes: int,
        expire_seconds: int,
        max_file_size_bytes: int,
        download_timeout_seconds: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir).expanduser().resolve()
        self.napcat_dir = napcat_dir.rstrip("/\\")
        self.max_size_bytes = max_size_bytes
        self.expire_seconds = expire_seconds
        self.max_file_size_bytes = max_file_size_bytes
        self._state_lock = asyncio.Lock()
        self._download_states: dict[str, _DownloadState] = {}
        self._active_paths: dict[Path, int] = {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(download_timeout_seconds),
        )

    async def initialize(self) -> None:
        """创建缓存目录并清理过期文件和残留临时文件。"""
        await asyncio.to_thread(self.storage_dir.mkdir, parents=True, exist_ok=True)
        await self.cleanup()

    async def close(self) -> None:
        """关闭缓存使用的 HTTP 客户端。"""
        if self._owns_client:
            await self._client.aclose()

    async def get_or_download(self, platform: str, song_id: str, audio_url: str) -> Path:
        """返回已加发送保护的缓存文件；缓存不存在时下载远程 MP3。"""
        cache_key = self._build_cache_key(platform, song_id)
        target_path = self.storage_dir / f"{cache_key}.mp3"
        download_state = await self._acquire_download_state(cache_key)

        try:
            async with download_state.lock:
                target_key = target_path.absolute()
                async with self._state_lock:
                    if target_path.is_symlink():
                        await asyncio.to_thread(target_path.unlink)
                    cache_hit = await asyncio.to_thread(self._is_valid_cache_file, target_path)
                    if cache_hit:
                        await asyncio.to_thread(os.utime, target_path, None)
                    self._active_paths[target_key] = self._active_paths.get(target_key, 0) + 1

                if cache_hit:
                    return target_path

                try:
                    await self._download(platform, audio_url, target_path)
                    async with self._state_lock:
                        await asyncio.to_thread(self._enforce_size_limit_sync, target_path)
                    return target_path
                except BaseException:
                    await self.release(target_path)
                    raise
        finally:
            await self._release_download_state(cache_key, download_state)

    async def _acquire_download_state(self, cache_key: str) -> _DownloadState:
        async with self._state_lock:
            state = self._download_states.get(cache_key)
            if state is None:
                state = _DownloadState(lock=asyncio.Lock())
                self._download_states[cache_key] = state
            state.references += 1
            return state

    async def _release_download_state(self, cache_key: str, state: _DownloadState) -> None:
        async with self._state_lock:
            state.references -= 1
            if state.references == 0 and not state.lock.locked():
                self._download_states.pop(cache_key, None)

    async def release(self, cache_path: Path) -> None:
        """取消一次缓存文件发送保护。"""
        async with self._state_lock:
            cache_key = cache_path.absolute()
            reference_count = self._active_paths.get(cache_key, 0)
            if reference_count <= 1:
                self._active_paths.pop(cache_key, None)
            else:
                self._active_paths[cache_key] = reference_count - 1

    def napcat_path(self, cache_path: Path) -> str:
        """将缓存文件转换为 NapCat 进程可见的路径。"""
        return str(PurePosixPath(self.napcat_dir) / cache_path.name)

    async def cleanup(self) -> None:
        """删除过期缓存、残留临时文件，并执行容量淘汰。"""
        async with self._state_lock:
            cleanup_worker = asyncio.create_task(asyncio.to_thread(self._cleanup_sync))
            try:
                await asyncio.shield(cleanup_worker)
            except asyncio.CancelledError:
                # to_thread 的工作线程不会随协程取消而停止，必须等待实际清理完成，
                # 避免旧缓存实例在热更新后继续删除新实例正在使用的文件。
                await cleanup_worker
                raise

    async def enforce_size_limit(self, protected_path: Path | None = None) -> None:
        """缓存超过容量上限时按最近访问时间淘汰。"""
        async with self._state_lock:
            await asyncio.to_thread(self._enforce_size_limit_sync, protected_path)

    async def _download(self, platform: str, audio_url: str, target_path: Path) -> None:
        temp_path = target_path.with_suffix(".mp3.part")
        await asyncio.to_thread(temp_path.unlink, missing_ok=True)
        headers = _AUDIO_DOWNLOAD_HEADERS.get(platform, {})

        try:
            async with self._client.stream("GET", audio_url, headers=headers) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in _MP3_CONTENT_TYPES:
                    raise AudioCacheError(f"音频响应不是 MP3: {content_type}")

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise AudioCacheError("音频响应的 Content-Length 无效") from exc
                    if declared_size > self.max_file_size_bytes:
                        raise AudioCacheError("音频文件超过缓存单文件大小限制")

                total_size = 0
                with temp_path.open("wb") as file:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        total_size += len(chunk)
                        if total_size > self.max_file_size_bytes:
                            raise AudioCacheError("音频文件超过缓存单文件大小限制")
                        file.write(chunk)

            if total_size == 0 or not await asyncio.to_thread(self._is_valid_cache_file, temp_path):
                raise AudioCacheError("下载内容不是有效的 MP3 文件")

            await asyncio.to_thread(temp_path.replace, target_path)
        except BaseException:
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            raise

    def _cleanup_sync(self) -> None:
        now = time.time()
        for path in self.storage_dir.iterdir():
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            if not path.is_file():
                continue
            if path.name.endswith(".part"):
                if now - path.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
                continue
            if path.suffix.lower() != ".mp3":
                continue
            if path.absolute() in self._active_paths:
                continue
            if now - path.stat().st_mtime > self.expire_seconds:
                path.unlink(missing_ok=True)
        self._enforce_size_limit_sync()

    def _enforce_size_limit_sync(self, protected_path: Path | None = None) -> None:
        files = [
            path
            for path in self.storage_dir.iterdir()
            if not path.is_symlink() and path.is_file() and path.suffix.lower() == ".mp3"
        ]
        total_size = sum(path.stat().st_size for path in files)
        if total_size <= self.max_size_bytes:
            return

        protected_absolute = protected_path.absolute() if protected_path else None
        files.sort(key=lambda path: path.stat().st_mtime)
        for path in files:
            absolute_path = path.absolute()
            if absolute_path in self._active_paths:
                continue
            if protected_absolute is not None and absolute_path == protected_absolute:
                continue
            file_size = path.stat().st_size
            path.unlink(missing_ok=True)
            total_size -= file_size
            if total_size <= self.max_size_bytes:
                break

    @staticmethod
    def _build_cache_key(platform: str, song_id: str) -> str:
        safe_platform = _SAFE_NAME_PATTERN.sub("_", platform).strip("_") or "music"
        safe_song_id = _SAFE_NAME_PATTERN.sub("_", song_id).strip("_")
        if not safe_song_id:
            safe_song_id = hashlib.sha256(song_id.encode()).hexdigest()[:16]
        return f"{safe_platform}_{safe_song_id}"

    @staticmethod
    def _is_valid_cache_file(path: Path) -> bool:
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 4:
            return False

        with path.open("rb") as file:
            header = file.read(10)
            frame_offset = 0
            if header.startswith(b"ID3"):
                if len(header) < 10 or any(value & 0x80 for value in header[6:10]):
                    return False
                tag_size = (
                    (header[6] << 21)
                    | (header[7] << 14)
                    | (header[8] << 7)
                    | header[9]
                )
                frame_offset = 10 + tag_size
                if header[5] & 0x10:
                    frame_offset += 10
            file.seek(frame_offset)
            frame_data = file.read(2048)

        first_frame = MusicAudioCache._mpeg_layer3_frame_info(frame_data[:4])
        if first_frame is None or len(frame_data) < first_frame.length + 4:
            return False

        second_frame = MusicAudioCache._mpeg_layer3_frame_info(
            frame_data[first_frame.length : first_frame.length + 4]
        )
        return (
            second_frame is not None
            and second_frame.version_id == first_frame.version_id
            and second_frame.sample_rate_index == first_frame.sample_rate_index
        )

    @staticmethod
    def _mpeg_layer3_frame_info(header: bytes) -> _MpegFrameInfo | None:
        if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
            return None

        version_id = (header[1] >> 3) & 0x03
        layer_id = (header[1] >> 1) & 0x03
        bitrate_index = (header[2] >> 4) & 0x0F
        sample_rate_index = (header[2] >> 2) & 0x03
        padding = (header[2] >> 1) & 0x01
        if version_id == 1 or layer_id != 1 or bitrate_index in (0, 15) or sample_rate_index == 3:
            return None

        if version_id == 3:
            bitrate = _MPEG1_LAYER3_BITRATES[bitrate_index] * 1000
            sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index]
            frame_length = (144 * bitrate) // sample_rate + padding
            return _MpegFrameInfo(frame_length, version_id, sample_rate_index)

        bitrate = _MPEG2_LAYER3_BITRATES[bitrate_index] * 1000
        divisor = 2 if version_id == 2 else 4
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index] // divisor
        frame_length = (72 * bitrate) // sample_rate + padding
        return _MpegFrameInfo(frame_length, version_id, sample_rate_index)
