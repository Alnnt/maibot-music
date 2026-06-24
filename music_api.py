"""音乐搜索 API 客户端 — 支持网易云音乐和QQ音乐。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("maibot-music.api")

# 网络请求超时（秒）
_REQUEST_TIMEOUT = 10

# 网易云音乐 eapi 加密密钥（16 字节 AES-128-ECB）
_EAPI_KEY = b"e82ckenh8dichen8"

# 网易云音乐请求头 — Web 端
_NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

# 网易云音乐请求头 — 移动端 eapi
_EAPI_HEADERS = {
    "User-Agent": "NeteaseMusic/9.1.65.240916182646(9001065);Dalvik/2.1.0 (Linux; U; Android 14)",
    "Referer": "/api/song/enhance/player/url",
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
    media_id: str = ""  # QQ音乐的 strMediaMid（用于构造播放URL的filename）

    def display(self) -> str:
        """返回可读的歌曲描述。"""
        parts = [self.name]
        if self.artists:
            parts.append(f"- {self.artists}")
        if self.album:
            parts.append(f"({self.album})")
        return " ".join(parts)


# ===== eapi 加密工具 =====


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 加密，PKCS7 填充。"""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    return encryptor.update(padded) + encryptor.finalize()


def _eapi_encrypt(url: str, params: dict[str, Any]) -> str:
    """网易云音乐 eapi 加密。

    Args:
        url: API 路径，如 /api/song/enhance/player/url
        params: 请求参数字典。

    Returns:
        加密后的请求参数字符串。
    """
    data_text = json.dumps(params, separators=(",", ":"), ensure_ascii=False)

    # 签名：nobody{url}use{data}md5forencrypt → MD5 → 拼接 → 加密
    sign_src = f"nobody{url}use{data_text}md5forencrypt"
    md5_hash = hashlib.md5(sign_src.encode()).hexdigest()
    sign_text = f"{url}-36cd479b6b5-{data_text}-36cd479b6b5-{md5_hash}"

    enc_text = _aes_ecb_encrypt(_EAPI_KEY, sign_text.encode()).hex().upper()
    # 网易云 API 要求 hex 编码使用大写字母
    return enc_text


class MusicSearchClient:
    """音乐搜索客户端，支持网易云和QQ音乐。

    Args:
        netease_cookie: 网易云音乐 Cookie，形如 {"MUSIC_U": "...", "__csrf": "..."}。
        qq_cookie: QQ音乐 Cookie，形如 {"uin": "...", "qqmusic_key": "..."}。
        napcat_url: NapCat HTTP API 地址（用于获取消息原始 JSON 解析音乐卡片）。
        napcat_token: NapCat HTTP API 访问令牌。
    """

    def __init__(
        self,
        netease_cookie: dict[str, str] | None = None,
        qq_cookie: dict[str, str] | None = None,
        napcat_url: str = "",
        napcat_token: str = "",
    ) -> None:
        self._netease_cookie = netease_cookie or {}
        self._qq_cookie = qq_cookie or {}
        self._napcat_url = napcat_url.strip().rstrip("/")
        self._napcat_token = napcat_token.strip()

        # 将用户提供的 Cookie 注入到 HTTP 客户端
        netease_cookies = {}
        if self._netease_cookie.get("MUSIC_U"):
            netease_cookies["MUSIC_U"] = self._netease_cookie["MUSIC_U"]
        if self._netease_cookie.get("__csrf"):
            netease_cookies["__csrf"] = self._netease_cookie["__csrf"]

        qq_cookies = {}
        if self._qq_cookie.get("uin"):
            qq_cookies["uin"] = self._qq_cookie["uin"]
        if self._qq_cookie.get("qqmusic_key"):
            qq_cookies["qqmusic_key"] = self._qq_cookie["qqmusic_key"]

        self._netease_client = httpx.AsyncClient(
            headers=_NETEASE_HEADERS,
            cookies=netease_cookies,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        self._eapi_client = httpx.AsyncClient(
            headers=_EAPI_HEADERS,
            cookies=netease_cookies,
            timeout=_REQUEST_TIMEOUT,
        )
        self._qq_client = httpx.AsyncClient(
            headers=_QQ_HEADERS,
            cookies=qq_cookies,
            timeout=_REQUEST_TIMEOUT,
        )
        # NapCat HTTP API 客户端（用于获取消息原始 JSON 解析音乐卡片）
        napcat_headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._napcat_token:
            napcat_headers["Authorization"] = f"Bearer {self._napcat_token}"
        self._napcat_client: httpx.AsyncClient | None = None
        if self._napcat_url:
            self._napcat_client = httpx.AsyncClient(
                base_url=self._napcat_url,
                headers=napcat_headers,
                timeout=_REQUEST_TIMEOUT,
            )

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        for client in (
            self._netease_client,
            self._eapi_client,
            self._qq_client,
            self._napcat_client,
        ):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:
                pass

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
            # strMediaMid 可能和 songmid 不同，用于构造播放URL
            media_mid = str(song.get("strMediaMid", "") or song.get("media_mid", "") or song_mid)
            if song_mid and name:
                results.append(
                    SongInfo(
                        song_id=song_mid,
                        name=name,
                        artists=artists,
                        album=album,
                        platform="qq",
                        media_id=media_mid,
                    )
                )
        return results

    # ===== 歌曲详情 =====

    async def get_qq_song_detail(self, song_mid: str) -> SongInfo | None:
        """通过 song_mid 查询 QQ 音乐歌曲详情，获取 strMediaMid 等信息。

        Args:
            song_mid: 歌曲 mid。

        Returns:
            SongInfo，查询失败返回 None。
        """
        req_data = {
            "req_0": {
                "module": "music.pf_song_detail_svr",
                "method": "get_song_detail_yqq",
                "param": {"song_mid": song_mid},
            },
            "comm": {
                "format": "json",
                "ct": 19,
                "cv": 0,
            },
        }

        try:
            resp = await self._qq_client.post(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                json=req_data,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("QQ音乐歌曲详情查询超时: %s", song_mid)
            return None
        except Exception:
            logger.exception("QQ音乐歌曲详情查询异常: %s", song_mid)
            return None

        try:
            track = data["req_0"]["data"]["track_info"]
        except (KeyError, IndexError, TypeError):
            logger.debug("QQ音乐歌曲详情解析失败: %s", song_mid)
            return None

        mid = str(track.get("mid", "") or song_mid)
        name = str(track.get("name", ""))
        singers = track.get("singer", [])
        artists = ", ".join(s.get("name", "") for s in singers if s.get("name"))
        album = str(track.get("album", {}).get("name", ""))
        media_mid = str(track.get("file", {}).get("media_mid", "") or mid)

        if not mid or not name:
            return None

        return SongInfo(
            song_id=mid,
            name=name,
            artists=artists,
            album=album,
            platform="qq",
            media_id=media_mid,
        )

    # ===== 获取音频 URL =====

    async def get_song_url(self, song_id: str, platform: str, media_id: str = "") -> str | None:
        """获取歌曲的可播放音频 URL。

        Args:
            song_id: 歌曲ID。
            platform: 音乐平台，"163" 或 "qq"。
            media_id: QQ音乐的 strMediaMid（用于构造播放URL的filename）。

        Returns:
            音频 URL，获取失败返回 None。
        """
        if platform == "qq":
            return await self._get_qq_song_url(song_id, media_id)
        return await self._get_netease_song_url(song_id)

    async def _get_netease_song_url(self, song_id: str) -> str | None:
        """获取网易云音乐歌曲的播放 URL。

        依次尝试：
        1. eapi /api/song/enhance/player/url (移动端加密接口，付费歌曲首选)
        2. /api/song/enhance/player/url (标准 Web 接口，有登录态可获取高音质)
        3. /song/media/outer/url (直链重定向，兜底)

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        # 1. eapi 加密接口 — 对免费和付费歌曲都更友好
        url = await self._get_netease_eapi_url(song_id)
        if url:
            return url

        # 2. 标准 Web 接口
        url = await self._get_netease_web_url(song_id)
        if url:
            return url

        # 3. 直链重定向兜底
        url = await self._get_netease_direct_url(song_id)
        if url:
            return url

        return None

    async def _get_netease_eapi_url(self, song_id: str) -> str | None:
        """通过 eapi 加密接口获取网易云歌曲播放 URL。

        eapi 是移动端接口，对付费/VIP 歌曲支持更好。
        需要有 MUSIC_U 登录态。

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        api_path = "/api/song/enhance/player/url"
        csrf_token = self._netease_cookie.get("__csrf", "")

        params: dict[str, Any] = {
            "ids": f"[{song_id}]",
            "br": 999000,
            "csrf_token": csrf_token,
        }

        try:
            enc_params = _eapi_encrypt(api_path, params)
            resp = await self._eapi_client.post(
                f"https://interface.music.163.com/eapi{api_path}",
                data={"params": enc_params},
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug("网易云eapi响应: song_id=%s code=%s", song_id, data.get("code"))
            url_list = data.get("data", [])
            if url_list and isinstance(url_list, list):
                url = str(url_list[0].get("url", "") or "").strip()
                if url:
                    return url
                logger.debug(
                    "网易云eapi返回空URL: song_id=%s code=%s",
                    song_id,
                    url_list[0].get("code"),
                )
        except httpx.TimeoutException:
            logger.warning("网易云eapi获取播放URL超时: %s", song_id)
        except Exception:
            logger.warning("网易云eapi获取失败: %s", song_id)

        return None

    async def _get_netease_web_url(self, song_id: str) -> str | None:
        """通过标准 Web 接口获取网易云歌曲播放 URL。

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        params: dict[str, str] = {"ids": f"[{song_id}]", "br": "999000"}
        csrf_token = self._netease_cookie.get("__csrf", "")
        if csrf_token:
            params["csrf_token"] = csrf_token

        try:
            resp = await self._netease_client.get(
                "https://music.163.com/api/song/enhance/player/url",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug("网易云标准接口响应: song_id=%s code=%s", song_id, data.get("code"))
            url_list = data.get("data", [])
            if url_list and isinstance(url_list, list):
                url = str(url_list[0].get("url", "") or "").strip()
                if url:
                    return url
                logger.debug(
                    "网易云标准接口返回空URL: song_id=%s code=%s",
                    song_id,
                    url_list[0].get("code"),
                )
        except httpx.TimeoutException:
            logger.warning("网易云音乐获取播放URL超时: %s", song_id)
        except Exception:
            logger.warning("网易云音乐标准接口获取失败: %s", song_id)

        return None

    async def _get_netease_direct_url(self, song_id: str) -> str | None:
        """通过直链重定向获取网易云歌曲播放 URL。

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        try:
            resp = await self._netease_client.get(
                f"https://music.163.com/song/media/outer/url?id={song_id}.mp3",
                follow_redirects=True,
            )
            final_url = str(resp.url)
            logger.debug("网易云直链重定向: song_id=%s final_url=%s", song_id, final_url)
            if final_url and any(ext in final_url for ext in (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")):
                return final_url
        except Exception:
            logger.debug("网易云音乐直链获取失败: %s", song_id)

        return None

    async def _get_qq_song_url(self, song_mid: str, media_mid: str = "") -> str | None:
        """获取QQ音乐歌曲的播放 URL。

        使用 vkey.GetVkeyServer 接口，一次性请求所有音质等级，
        按优先级返回第一个有效的播放链接。

        Args:
            song_mid: 歌曲 mid。
            media_mid: 歌曲 strMediaMid，用于构造 filename。
                许多歌曲的 media_mid 与 song_mid 不同，
                使用错误的 mid 会导致有版权的歌也无法获取播放链接。

        Returns:
            音频 URL，获取失败返回 None。
        """
        if not media_mid:
            media_mid = song_mid
        # 按音质从高到低构造 filename 列表：
        # F000 = FLAC 无损, M800 = 320kbps MP3,
        # M500 = 128kbps MP3, C400 = 96kbps M4A
        # filename 格式: {prefix}{songmid}{mediaMid}{ext}
        quality_prefixes = [
            ("F000", ".flac"),
            ("M800", ".mp3"),
            ("M500", ".mp3"),
            ("C400", ".m4a"),
        ]
        filenames = [f"{prefix}{song_mid}{media_mid}{ext}" for prefix, ext in quality_prefixes]
        return await self._get_qq_vkey_batch(filenames, song_mid)

    async def _get_qq_vkey_batch(self, filenames: list[str], song_mid: str) -> str | None:
        """通过 QQ 音乐 vkey 接口批量获取播放 URL，按音质优先级返回第一个有效链接。

        Args:
            filenames: 按音质从高到低排列的文件名列表。
            song_mid: 歌曲 mid。

        Returns:
            音频 URL，获取失败返回 None。
        """
        guid = str(secrets.randbelow(9000000000) + 1000000000)

        # 从配置获取登录态
        uin = self._qq_cookie.get("uin", "0")
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "")
        loginflag = 1 if uin != "0" else 0

        # 一次请求发送所有音质 filename，songmid 也对应扩展
        req_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": filenames,
                    "guid": guid,
                    "songmid": [song_mid] * len(filenames),
                    "songtype": [0] * len(filenames),
                    "uin": uin,
                    "loginflag": loginflag,
                    "platform": "20",
                },
            },
            "loginUin": uin,
            "comm": {
                "uin": uin,
                "format": "json",
                "ct": 19,
                "cv": 0,
                "authst": qqmusic_key,
            },
        }

        try:
            resp = await self._qq_client.post(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                json=req_data,
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
            midurlinfo = data["req_0"]["data"]["midurlinfo"]
        except (KeyError, IndexError):
            logger.debug("QQ音乐vkey响应解析失败: %s", song_mid)
            return None

        # 选择非 ws.stream 的域名（优先 HTTPS）
        domain = ""
        for s in sip:
            if s.startswith("https://"):
                domain = s
                break
        if not domain and sip:
            domain = sip[0]

        # 按音质优先级遍历，返回第一个有效的播放链接
        for info in midurlinfo:
            purl = info.get("purl", "") if isinstance(info, dict) else ""
            if purl:
                return f"{domain}{purl}"

        return None

    # ===== 辅助方法 =====

    async def resolve_short_url(self, url: str) -> str | None:
        """解析音乐短链接，获取重定向后的完整 URL。

        对于 HTTP 302 重定向（如 c6.y.qq.com），只读取 Location header，
        不下载页面内容。对于 JS 重定向（如 163cn.tv），需要下载 HTML 解析。

        Args:
            url: 短链接地址。

        Returns:
            重定向后的最终 URL，失败返回 None。
        """
        # 根据域名选择正确的 HTTP 客户端
        if "y.qq.com" in url:
            client = self._qq_client
        else:
            client = self._netease_client

        # 先尝试只读 Location header（不下载页面）
        try:
            resp = await client.get(url, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if location:
                    logger.debug("短链接HTTP重定向: %s → %s", url, location)
                    return location
        except Exception:
            logger.debug("短链接HTTP重定向解析失败: %s", url)

        # HTTP 重定向未获取到，尝试跟随重定向后从最终 URL 或 HTML 中提取
        try:
            resp = await client.get(url, follow_redirects=True)
            final_url = str(resp.url)

            # 如果最终 URL 已包含 music.163.com 或 y.qq.com，重定向成功
            if "music.163.com" in final_url or "y.qq.com" in final_url:
                return final_url

            # 尝试从 HTML 内容中提取跳转 URL（163cn.tv JS 重定向的情况）
            html = resp.text
            meta_match = re.search(
                r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\']?\d+;\s*url=([^"\'>\s]+)',
                html,
                re.IGNORECASE,
            )
            if meta_match:
                return meta_match.group(1).strip()
            js_match = re.search(
                r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                html,
            )
            if js_match:
                return js_match.group(1).strip()

            return final_url
        except Exception:
            logger.debug("短链接解析失败: %s", url)
            return None

    async def get_raw_message(self, message_id: int) -> dict[str, Any] | None:
        """通过 NapCat HTTP API 获取消息原始 JSON。

        Args:
            message_id: 消息 ID。

        Returns:
            响应 data 字段，失败返回 None。
        """
        if self._napcat_client is None:
            return None

        try:
            resp = await self._napcat_client.post(
                "/get_msg",
                json={"message_id": message_id},
            )
            resp.raise_for_status()
            raw_detail = resp.json()
        except Exception:
            logger.debug("调用 NapCat get_msg 失败: %s", message_id)
            return None

        data = raw_detail.get("data") if isinstance(raw_detail, dict) else None
        if not isinstance(data, dict):
            return None

        return data
