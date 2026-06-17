"""音乐搜索 API 客户端 — 支持网易云音乐和QQ音乐。"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("maibot-music.api")

# 网络请求超时（秒）
_REQUEST_TIMEOUT = 10

# 网易云音乐请求头
_NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

# QQ音乐请求头
_QQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://y.qq.com/",
}


@dataclass
class SongInfo:
    """歌曲信息。"""

    song_id: str  # 歌曲ID（网易云为数字ID，QQ音乐为mid）
    name: str  # 歌曲名
    artists: str  # 歌手名（逗号分隔）
    album: str  # 专辑名
    platform: str  # "163" 或 "qq"

    def display(self) -> str:
        """返回可读的歌曲描述。"""
        parts = [self.name]
        if self.artists:
            parts.append(f"- {self.artists}")
        if self.album:
            parts.append(f"({self.album})")
        return " ".join(parts)


class MusicSearchClient:
    """音乐搜索客户端，支持网易云和QQ音乐。"""

    def __init__(self) -> None:
        self._netease_client = httpx.AsyncClient(
            headers=_NETEASE_HEADERS,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        self._qq_client = httpx.AsyncClient(
            headers=_QQ_HEADERS,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self._netease_client.aclose()
        await self._qq_client.aclose()

    async def search(self, query: str, platform: str, limit: int = 5) -> list[SongInfo]:
        """搜索歌曲。

        Args:
            query: 搜索关键词。
            platform: 音乐平台，"163" 或 "qq"。
            limit: 返回结果数量上限。

        Returns:
            歌曲信息列表。
        """
        if platform == "qq":
            return await self.search_qq(query, limit)
        return await self.search_netease(query, limit)

    async def search_netease(self, query: str, limit: int = 5) -> list[SongInfo]:
        """搜索网易云音乐。

        Args:
            query: 搜索关键词。
            limit: 返回结果数量上限。

        Returns:
            歌曲信息列表。
        """
        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/search/get/web",
                params={"s": query, "type": "1", "limit": str(limit), "offset": "0"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("网易云音乐搜索超时: %s", query)
            return []
        except httpx.HTTPStatusError as e:
            logger.warning("网易云音乐搜索请求失败: %s %s", e.response.status_code, query)
            return []
        except Exception:
            logger.exception("网易云音乐搜索异常: %s", query)
            return []

        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return []

        results: list[SongInfo] = []
        for song in songs:
            song_id = str(song.get("id", ""))
            name = str(song.get("name", ""))
            artists = ", ".join(
                artist.get("name", "") for artist in song.get("artists", []) if artist.get("name")
            )
            album = str(song.get("album", {}).get("name", ""))
            if song_id and name:
                results.append(
                    SongInfo(
                        song_id=song_id,
                        name=name,
                        artists=artists,
                        album=album,
                        platform="163",
                    )
                )
        return results

    async def search_qq(self, query: str, limit: int = 5) -> list[SongInfo]:
        """搜索QQ音乐。

        Args:
            query: 搜索关键词。
            limit: 返回结果数量上限。

        Returns:
            歌曲信息列表。
        """
        try:
            resp = await self._qq_client.get(
                "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                params={"w": query, "format": "json", "p": "1", "n": str(limit)},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("QQ音乐搜索超时: %s", query)
            return []
        except httpx.HTTPStatusError as e:
            logger.warning("QQ音乐搜索请求失败: %s %s", e.response.status_code, query)
            return []
        except Exception:
            logger.exception("QQ音乐搜索异常: %s", query)
            return []

        song_list = data.get("data", {}).get("song", {}).get("list", [])
        if not song_list:
            return []

        results: list[SongInfo] = []
        for song in song_list:
            song_mid = str(song.get("songmid", "") or song.get("mid", ""))
            name = str(song.get("songname", "") or song.get("name", ""))
            singers = song.get("singer", [])
            artists = ", ".join(s.get("name", "") for s in singers if s.get("name"))
            album = str(song.get("albumname", "") or song.get("album", {}).get("name", ""))
            if song_mid and name:
                results.append(
                    SongInfo(
                        song_id=song_mid,
                        name=name,
                        artists=artists,
                        album=album,
                        platform="qq",
                    )
                )
        return results

    # ===== 获取音频 URL =====

    async def get_song_url(self, song_id: str, platform: str) -> str | None:
        """获取歌曲的可播放音频 URL。

        Args:
            song_id: 歌曲ID。
            platform: 音乐平台，"163" 或 "qq"。

        Returns:
            音频 URL，获取失败返回 None。
        """
        if platform == "qq":
            return await self._get_qq_song_url(song_id)
        return await self._get_netease_song_url(song_id)

    async def _get_netease_song_url(self, song_id: str) -> str | None:
        """获取网易云音乐歌曲的播放 URL。

        使用 /api/song/enhance/player/url 接口。

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/song/enhance/player/url",
                params={"ids": f"[{song_id}]", "br": "320000"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("网易云音乐获取播放URL超时: %s", song_id)
            return None
        except Exception:
            logger.exception("网易云音乐获取播放URL异常: %s", song_id)
            return None

        url_list = data.get("data", [])
        if url_list and isinstance(url_list, list):
            url = str(url_list[0].get("url", "") or "").strip()
            if url:
                return url

        return None

    async def _get_qq_song_url(self, song_mid: str) -> str | None:
        """获取QQ音乐歌曲的播放 URL。

        使用 vkey.GetVkeyServer 接口。

        Args:
            song_mid: 歌曲 mid。

        Returns:
            音频 URL，获取失败返回 None。
        """
        # 构造 filename: M500<songmid><songmid>.mp3 (128kbps)
        # 优先尝试 M800(320k)，失败再试 M500(128k)
        for prefix, ext in [("M800", ".mp3"), ("M500", ".mp3"), ("C400", ".m4a")]:
            filename = f"{prefix}{song_mid}{song_mid}{ext}"
            url = await self._get_qq_vkey(filename, song_mid)
            if url:
                return url

        return None

    async def _get_qq_vkey(self, filename: str, song_mid: str) -> str | None:
        """通过 QQ 音乐 vkey 接口获取播放 URL。

        Args:
            filename: 构造的文件名。
            song_mid: 歌曲 mid。

        Returns:
            音频 URL，获取失败返回 None。
        """
        guid = str(int(random.random() * 2147483647 * time.time()) % 10000000000)

        req_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": [filename],
                    "guid": guid,
                    "songmid": [song_mid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "loginUin": "0",
            "comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0},
        }

        try:
            resp = await self._qq_client.get(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                params={
                    "format": "json",
                    "data": quote(
                        __import__("json").dumps(req_data, separators=(",", ":")),
                        safe="",
                    ),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("QQ音乐获取vkey超时: %s", song_mid)
            return None
        except Exception:
            logger.exception("QQ音乐获取vkey异常: %s", song_mid)
            return None

        try:
            sip = data["req_0"]["data"]["sip"]
            purl = data["req_0"]["data"]["midurlinfo"][0]["purl"]
        except (KeyError, IndexError):
            logger.debug("QQ音乐vkey响应解析失败: %s", song_mid)
            return None

        if not purl:
            return None

        # 选择非 ws.stream 的域名（优先 HTTPS）
        domain = sip[0] if sip else ""
        for s in sip:
            if s.startswith("https://"):
                domain = s
                break
        if not domain:
            domain = sip[0]

        return f"{domain}{purl}"

    # ===== 辅助方法 =====

    async def resolve_short_url(self, url: str) -> str | None:
        """解析网易云音乐短链接，获取重定向后的完整 URL。

        Args:
            url: 短链接地址。

        Returns:
            重定向后的 URL，失败返回 None。
        """
        try:
            resp = await self._netease_client.get(url, follow_redirects=True)
            return str(resp.url)
        except Exception:
            logger.debug("短链接解析失败: %s", url)
            return None
