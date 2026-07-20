from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "music_api.py"
_SPEC = importlib.util.spec_from_file_location("maibot_music_api_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
MusicSearchClient = _MODULE.MusicSearchClient


@pytest.mark.asyncio
async def test_qq_local_mode_requests_only_mp3_candidates() -> None:
    client = object.__new__(MusicSearchClient)
    client._get_qq_vkey_batch = AsyncMock(return_value="https://example.test/song.mp3")

    await client._get_qq_song_url("song-mid", "media-mid", mp3_only=True)

    client._get_qq_vkey_batch.assert_awaited_once_with(
        [
            "M800song-midmedia-mid.mp3",
            "M500song-midmedia-mid.mp3",
        ],
        "song-mid",
    )


@pytest.mark.asyncio
async def test_qq_remote_mode_preserves_all_candidates() -> None:
    client = object.__new__(MusicSearchClient)
    client._get_qq_vkey_batch = AsyncMock(return_value="https://example.test/song.flac")

    await client._get_qq_song_url("song-mid", "media-mid", mp3_only=False)

    client._get_qq_vkey_batch.assert_awaited_once_with(
        [
            "F000song-midmedia-mid.flac",
            "M800song-midmedia-mid.mp3",
            "M500song-midmedia-mid.mp3",
            "C400song-midmedia-mid.m4a",
        ],
        "song-mid",
    )
