"""音乐搜索 API 客户端 — 支持网易云音乐和QQ音乐。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import hashlib
import json
import logging
import re
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx

logger = logging.getLogger("maibot-music.api")

# 网络请求超时（秒）
_REQUEST_TIMEOUT = 10

# 上游异常文本的日志预览长度，避免异常响应淹没日志
_RESPONSE_ERROR_PREVIEW_LENGTH = 512

# 上游响应中可能包含凭据的字段名片段
_SENSITIVE_RESPONSE_KEY_PARTS = (
    "authst",
    "authorization",
    "cookie",
    "csrf",
    "credential",
    "key",
    "music_a",
    "music_u",
    "password",
    "session",
    "secret",
    "signature",
    "ticket",
    "token",
)

_SENSITIVE_TEXT_KEYS = (
    r"authst|authorization|cookie|csrf|credential|qqmusic_key|qm_keyst|p_skey|skey|key|music_a|music_u|"
    r"password|session|secret|signature|ticket|token"
)

_AUTHORIZATION_TEXT_PATTERN = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<value>(?:basic|bearer|digest)\s+[^\s,;]+|[^\s,;]+)"
)

_COOKIE_HEADER_TEXT_PATTERN = re.compile(
    r"(?im)(?P<prefix>(?<![\"'])\b(?:cookie|set-cookie)\s*:\s*)(?P<value>[^\r\n]+)"
)

_SENSITIVE_TEXT_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?<![\w])(?P<prefix>[\"']?(?:{_SENSITIVE_TEXT_KEYS})[\"']?\s*[:=]\s*)"
)

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


class MusicAPIResponseError(RuntimeError):
    """音乐平台返回的响应不符合预期协议。"""

    def __init__(self, message: str, *, response_detail: str = "") -> None:
        super().__init__(message)
        self.response_detail = response_detail

    def diagnostic_message(self) -> str:
        """返回适合写入独立错误日志的完整诊断信息。"""
        if not self.response_detail:
            return str(self)
        return f"{self} response={self.response_detail}"


def _sanitize_response_value(value: Any) -> Any:
    """递归移除上游响应中可能包含的凭据。"""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower()
            if any(part in normalized_key for part in _SENSITIVE_RESPONSE_KEY_PARTS):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize_response_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_response_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_response_text(value)
    return value


def _sanitize_response_text(value: str) -> str:
    """移除非 JSON 响应文本中的常见凭据。"""
    sanitized = _AUTHORIZATION_TEXT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        value,
    )
    sanitized = _COOKIE_HEADER_TEXT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    return _sanitize_text_assignments(sanitized)


def _sanitize_text_assignments(value: str) -> str:
    """扫描敏感字段赋值，完整遮蔽引号、数组和对象形式的值。"""
    parts: list[str] = []
    cursor = 0
    while match := _SENSITIVE_TEXT_ASSIGNMENT_PATTERN.search(value, cursor):
        value_start = match.end()
        replacement, value_end = _redact_text_value(value, value_start)
        parts.append(value[cursor:value_start])
        parts.append(replacement)
        cursor = value_end
    parts.append(value[cursor:])
    return "".join(parts)


def _redact_text_value(value: str, start: int) -> tuple[str, int]:
    """返回单个文本赋值的脱敏结果及原值结束位置。"""
    if start >= len(value):
        return "[REDACTED]", start

    first = value[start]
    if first in ("\"", "'"):
        end = _quoted_text_value_end(value, start, first)
        if end is None:
            return "[REDACTED]", _line_end(value, start)
        return f"{first}[REDACTED]{first}", end

    if first in ("[", "{"):
        end = _structured_text_value_end(value, start)
        return "[REDACTED]", end if end is not None else _line_end(value, start)

    end = start
    while end < len(value) and value[end] not in "\"'&,;\r\n\t }]":
        end += 1
    return "[REDACTED]", end


def _quoted_text_value_end(value: str, start: int, quote: str) -> int | None:
    """定位支持反斜杠转义的引号值结尾。"""
    escaped = False
    for index in range(start + 1, len(value)):
        char = value[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return index + 1
    return None


def _structured_text_value_end(value: str, start: int) -> int | None:
    """定位可能嵌套且包含字符串的数组或对象值结尾。"""
    expected_closers: list[str] = []
    quote = ""
    escaped = False
    pairs = {"[": "]", "{": "}"}

    for index in range(start, len(value)):
        char = value[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue

        if char in ("\"", "'"):
            quote = char
        elif char in pairs:
            expected_closers.append(pairs[char])
        elif expected_closers and char == expected_closers[-1]:
            expected_closers.pop()
            if not expected_closers:
                return index + 1

    return None


def _line_end(value: str, start: int) -> int:
    """返回当前行的结束位置，供无法确定值边界时保守脱敏。"""
    candidates = [position for marker in ("\r", "\n") if (position := value.find(marker, start)) >= 0]
    return min(candidates, default=len(value))


def _response_detail(value: Any) -> str:
    """将完整上游响应转换为脱敏的单行日志文本。"""
    sanitized = _sanitize_response_value(value)
    try:
        return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(sanitized)


def _http_response_detail(response: httpx.Response) -> str:
    """读取无法解析为 JSON 的 HTTP 响应文本。"""
    try:
        return _response_detail(response.json())
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    try:
        return _response_detail(response.text)
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        return "<response body unavailable>"


def _request_error_detail(exc: httpx.RequestError) -> str:
    """完整保留网络错误类型和原因，同时脱敏日志内容。"""
    return _response_detail(f"{type(exc).__name__}: {exc}")


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
        qq_cookie: QQ音乐登录 Cookie，形如 {"uin": "...", "qqmusic_key": "..."}。
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
        self._qq_cookie = {key: value.strip() for key, value in (qq_cookie or {}).items()}
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
        # QQ 音乐搜索、歌曲详情和播放资源请求都需要用户配置的登录态。
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
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(
                f"网易云音乐搜索超时: query={query!r}",
                response_detail=_request_error_detail(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise MusicAPIResponseError(
                f"网易云音乐搜索请求失败: query={query!r} status={exc.response.status_code}",
                response_detail=_http_response_detail(exc.response),
            ) from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(
                f"网易云音乐搜索网络异常: query={query!r}",
                response_detail=_request_error_detail(exc),
            ) from exc

        try:
            data = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MusicAPIResponseError(
                f"网易云音乐搜索响应不是有效 JSON: query={query!r}",
                response_detail=_http_response_detail(resp),
            ) from exc

        if not isinstance(data, dict):
            raise MusicAPIResponseError(
                f"网易云音乐搜索响应不是对象: query={query!r} type={type(data).__name__}",
                response_detail=_response_detail(data),
            )

        code = data.get("code")
        if code != 200:
            raise MusicAPIResponseError(
                f"网易云音乐搜索业务失败: query={query!r} code={code!r}",
                response_detail=_response_detail(data),
            )

        result = data.get("result")
        if not isinstance(result, dict):
            result_detail = ""
            if isinstance(result, str):
                result_preview = result[:_RESPONSE_ERROR_PREVIEW_LENGTH]
                result_detail = f" result={result_preview!r}"
                if len(result) > _RESPONSE_ERROR_PREVIEW_LENGTH:
                    result_detail += f" result_length={len(result)}"
            raise MusicAPIResponseError(
                "网易云音乐搜索响应格式错误: "
                f"query={query!r} result_type={type(result).__name__}{result_detail}",
                response_detail=_response_detail(data),
            )

        songs = result.get("songs", [])
        if not isinstance(songs, list):
            raise MusicAPIResponseError(
                "网易云音乐搜索 songs 不是列表: "
                f"query={query!r} type={type(songs).__name__}",
                response_detail=_response_detail(data),
            )
        if not songs:
            return []

        results: list[SongInfo] = []
        for song in songs:
            if not isinstance(song, dict):
                raise MusicAPIResponseError(
                    "网易云音乐搜索歌曲项不是对象: "
                    f"query={query!r} type={type(song).__name__}",
                    response_detail=_response_detail(data),
                )

            artists_data = song.get("artists", [])
            album_data = song.get("album", {})
            if not isinstance(artists_data, list) or not isinstance(album_data, dict):
                raise MusicAPIResponseError(
                    f"网易云音乐搜索歌曲字段格式错误: query={query!r}",
                    response_detail=_response_detail(data),
                )

            song_id = str(song.get("id", ""))
            name = str(song.get("name", ""))
            artists = ", ".join(
                str(artist.get("name", ""))
                for artist in artists_data
                if isinstance(artist, dict) and artist.get("name")
            )
            album = str(album_data.get("name", ""))
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

        使用 musicu.fcg 的 SearchCgiService 接口，与 vkey 请求同域，
        避免旧接口 client_search_cp 在部分网络环境下不可达。

        Args:
            query: 搜索关键词。
            limit: 返回结果数量上限。

        Returns:
            歌曲信息列表。
        """
        uin = self._qq_cookie.get("uin", "").strip()
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "").strip()
        missing_credentials: list[str] = []
        if not uin:
            missing_credentials.append("qq.uin")
        if not qqmusic_key:
            missing_credentials.append("qq.qqmusic_key")
        if missing_credentials:
            raise MusicAPIResponseError(
                f"QQ音乐搜索需要登录，缺少配置: {', '.join(missing_credentials)}"
            )

        req_data = {
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "search_type": 0,
                    "query": query,
                    "page_num": 1,
                    "num_per_page": limit,
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
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(
                f"QQ音乐搜索超时: query={query!r}",
                response_detail=_request_error_detail(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise MusicAPIResponseError(
                f"QQ音乐搜索请求失败: query={query!r} status={exc.response.status_code}",
                response_detail=_http_response_detail(exc.response),
            ) from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(
                f"QQ音乐搜索网络异常: query={query!r}",
                response_detail=_request_error_detail(exc),
            ) from exc

        try:
            data = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MusicAPIResponseError(
                f"QQ音乐搜索响应不是有效 JSON: query={query!r}",
                response_detail=_http_response_detail(resp),
            ) from exc

        if not isinstance(data, dict):
            raise MusicAPIResponseError(
                f"QQ音乐搜索响应不是对象: query={query!r} type={type(data).__name__}",
                response_detail=_response_detail(data),
            )

        req_result = data.get("req_1")
        if not isinstance(req_result, dict):
            raise MusicAPIResponseError(
                f"QQ音乐搜索响应缺少 req_1: query={query!r}",
                response_detail=_response_detail(data),
            )

        top_code = data.get("code")
        module_code = req_result.get("code")
        if top_code not in (None, 0) or module_code not in (None, 0):
            error_reason = (
                "QQ音乐搜索登录态失效或被拒绝"
                if module_code == 2001
                else "QQ音乐搜索业务失败"
            )
            raise MusicAPIResponseError(
                f"{error_reason}: "
                f"query={query!r} code={top_code!r} module_code={module_code!r}",
                response_detail=_response_detail(data),
            )

        result_data = req_result.get("data")
        body = result_data.get("body") if isinstance(result_data, dict) else None
        song_data = body.get("song") if isinstance(body, dict) else None
        song_list = song_data.get("list") if isinstance(song_data, dict) else None
        if not isinstance(song_list, list):
            raise MusicAPIResponseError(
                f"QQ音乐搜索响应歌曲列表格式错误: query={query!r} type={type(song_list).__name__}",
                response_detail=_response_detail(data),
            )

        if not song_list:
            return []

        results: list[SongInfo] = []
        for song in song_list:
            if not isinstance(song, dict):
                raise MusicAPIResponseError(
                    f"QQ音乐搜索歌曲项不是对象: query={query!r} type={type(song).__name__}",
                    response_detail=_response_detail(data),
                )

            singers = song.get("singer", [])
            album_data = song.get("album", {})
            file_data = song.get("file", {})
            if (
                not isinstance(singers, list)
                or not isinstance(album_data, dict)
                or not isinstance(file_data, dict)
            ):
                raise MusicAPIResponseError(
                    f"QQ音乐搜索歌曲字段格式错误: query={query!r}",
                    response_detail=_response_detail(data),
                )

            song_mid = str(song.get("mid", "") or song.get("songmid", ""))
            name = str(song.get("name", "") or song.get("songname", ""))
            artists = ", ".join(
                str(singer.get("name", ""))
                for singer in singers
                if isinstance(singer, dict) and singer.get("name")
            )
            album = str(album_data.get("name", "") or song.get("albumname", ""))
            media_mid = str(
                file_data.get("media_mid", "")
                or song.get("strMediaMid", "")
                or song.get("media_mid", "")
            )
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
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(f"QQ音乐歌曲详情查询超时: song_mid={song_mid}") from exc
        except httpx.HTTPStatusError as exc:
            raise MusicAPIResponseError(
                f"QQ音乐歌曲详情请求失败: song_mid={song_mid} status={exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(f"QQ音乐歌曲详情网络异常: song_mid={song_mid}") from exc

        try:
            data = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MusicAPIResponseError(
                f"QQ音乐歌曲详情响应不是有效 JSON: song_mid={song_mid}"
            ) from exc

        if not isinstance(data, dict):
            raise MusicAPIResponseError(
                f"QQ音乐歌曲详情响应不是对象: song_mid={song_mid} type={type(data).__name__}"
            )

        req_result = data.get("req_0")
        if not isinstance(req_result, dict):
            raise MusicAPIResponseError(f"QQ音乐歌曲详情响应缺少 req_0: song_mid={song_mid}")

        top_code = data.get("code")
        module_code = req_result.get("code")
        if top_code not in (None, 0) or module_code not in (None, 0):
            raise MusicAPIResponseError(
                "QQ音乐歌曲详情业务失败: "
                f"song_mid={song_mid} code={top_code!r} module_code={module_code!r}"
            )

        result_data = req_result.get("data")
        track = result_data.get("track_info") if isinstance(result_data, dict) else None
        if not isinstance(track, dict):
            raise MusicAPIResponseError(
                f"QQ音乐歌曲详情 track_info 不是对象: song_mid={song_mid} type={type(track).__name__}"
            )

        singers = track.get("singer", [])
        album_data = track.get("album", {})
        file_data = track.get("file", {})
        if (
            not isinstance(singers, list)
            or not isinstance(album_data, dict)
            or not isinstance(file_data, dict)
        ):
            raise MusicAPIResponseError(f"QQ音乐歌曲详情字段格式错误: song_mid={song_mid}")

        mid = str(track.get("mid", "") or song_mid)
        name = str(track.get("name", ""))
        artists = ", ".join(
            str(singer.get("name", ""))
            for singer in singers
            if isinstance(singer, dict) and singer.get("name")
        )
        album = str(album_data.get("name", ""))
        media_mid = str(file_data.get("media_mid", ""))

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

    async def get_song_url(
        self,
        song_id: str,
        platform: str,
        media_id: str = "",
        *,
        mp3_only: bool = False,
    ) -> str | None:
        """获取歌曲的可播放音频 URL。

        Args:
            song_id: 歌曲ID。
            platform: 音乐平台，"163" 或 "qq"。
            media_id: QQ音乐的 strMediaMid（用于构造播放URL的filename）。
            mp3_only: 是否只请求 MP3 音频，用于本地语音缓存。

        Returns:
            音频 URL，获取失败返回 None。
        """
        if platform == "qq":
            return await self._get_qq_song_url(song_id, media_id, mp3_only=mp3_only)
        return await self._get_netease_song_url(song_id, mp3_only=mp3_only)

    async def _get_netease_song_url(self, song_id: str, *, mp3_only: bool = False) -> str | None:
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
        bitrate = 320000 if mp3_only else 999000
        url = await self._get_netease_eapi_url(song_id, bitrate)
        if url:
            return url

        # 2. 标准 Web 接口
        url = await self._get_netease_web_url(song_id, bitrate)
        if url:
            return url

        # 3. 直链重定向兜底
        url = await self._get_netease_direct_url(song_id)
        if url:
            return url

        return None

    async def _get_netease_eapi_url(self, song_id: str, bitrate: int) -> str | None:
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
            "br": bitrate,
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

    async def _get_netease_web_url(self, song_id: str, bitrate: int) -> str | None:
        """通过标准 Web 接口获取网易云歌曲播放 URL。

        Args:
            song_id: 歌曲数字 ID。

        Returns:
            音频 URL，获取失败返回 None。
        """
        params: dict[str, str] = {"ids": f"[{song_id}]", "br": str(bitrate)}
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
            logger.debug(
                "网易云直链重定向: song_id=%s host=%s",
                song_id,
                urlparse(final_url).hostname or "",
            )
            if final_url and any(ext in final_url for ext in (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")):
                return final_url
        except Exception:
            logger.debug("网易云音乐直链获取失败: %s", song_id)

        return None

    async def _get_qq_song_url(
        self,
        song_mid: str,
        media_mid: str = "",
        *,
        mp3_only: bool = False,
    ) -> str | None:
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
        resource_mid = media_mid or song_mid
        # QQ 音频文件名使用资源 media_mid；songmid 仅作为 vkey 请求中的歌曲标识。
        quality_prefixes = [
            ("M800", ".mp3"),
            ("M500", ".mp3"),
        ]
        if not mp3_only:
            quality_prefixes = [
                ("F000", ".flac"),
                *quality_prefixes,
                ("C400", ".m4a"),
            ]
        filenames = [f"{prefix}{resource_mid}{ext}" for prefix, ext in quality_prefixes]
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

        # 从配置获取登录态。CgiGetVkey 在匿名请求中同样要求 loginflag=1。
        uin = self._qq_cookie.get("uin", "0")
        qqmusic_key = self._qq_cookie.get("qqmusic_key", "")

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
                    "loginflag": 1,
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

        if not isinstance(data, dict):
            logger.warning("QQ音乐vkey响应不是对象: song_mid=%s type=%s", song_mid, type(data).__name__)
            return None

        req_result = data.get("req_0")
        top_code = data.get("code")
        if not isinstance(req_result, dict):
            logger.warning("QQ音乐vkey响应缺少req_0: song_mid=%s code=%r", song_mid, top_code)
            return None

        module_code = req_result.get("code")
        if top_code not in (None, 0) or module_code not in (None, 0):
            logger.warning(
                "QQ音乐vkey业务失败: song_mid=%s code=%r module_code=%r",
                song_mid,
                top_code,
                module_code,
            )
            return None

        result_data = req_result.get("data")
        if not isinstance(result_data, dict):
            logger.warning("QQ音乐vkey数据不是对象: song_mid=%s", song_mid)
            return None

        sip = result_data.get("sip")
        midurlinfo = result_data.get("midurlinfo")
        if not isinstance(sip, list) or not isinstance(midurlinfo, list):
            logger.warning(
                "QQ音乐vkey响应字段类型错误: song_mid=%s sip=%s midurlinfo=%s",
                song_mid,
                type(sip).__name__,
                type(midurlinfo).__name__,
            )
            return None

        # 按 filename 映射响应项，避免上游调整响应顺序后选错音质。
        info_by_filename = {
            str(info.get("filename", "")): info
            for info in midurlinfo
            if isinstance(info, dict) and info.get("filename")
        }
        ordered_info = [info_by_filename.get(filename) for filename in filenames]
        if not info_by_filename and len(midurlinfo) == len(filenames):
            ordered_info = [info if isinstance(info, dict) else None for info in midurlinfo]

        for info in ordered_info:
            if not info:
                continue
            purl = str(info.get("purl", "") or "").strip()
            if not purl:
                continue

            if purl.startswith(("http://", "https://")):
                audio_url = purl
            else:
                domain = next(
                    (str(item) for item in sip if isinstance(item, str) and item.startswith("https://")),
                    "",
                )
                if not domain:
                    domain = next((str(item) for item in sip if isinstance(item, str) and item), "")
                if not domain:
                    logger.warning(
                        "QQ音乐vkey返回相对URL但没有播放域名，继续尝试下一音质: song_mid=%s",
                        song_mid,
                    )
                    continue
                audio_url = urljoin(domain, purl)

            parsed_url = urlparse(audio_url)
            if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
                return audio_url

            logger.warning("QQ音乐vkey返回无效音频URL，继续尝试下一音质: song_mid=%s", song_mid)

        diagnostics = [
            {
                "filename": str(info.get("filename", "")),
                "result": info.get("result"),
                "subcode": info.get("subcode"),
                "purl_present": bool(info.get("purl")),
            }
            for info in midurlinfo
            if isinstance(info, dict)
        ]
        logger.warning(
            "QQ音乐vkey未返回可用音频: song_mid=%s authenticated=%s candidates=%s response=%s",
            song_mid,
            bool(uin != "0" and qqmusic_key),
            filenames,
            diagnostics,
        )
        return None

    # ===== 辅助方法 =====

    # 短链接重定向允许的目标域名白名单
    _ALLOWED_REDIRECT_HOSTS = frozenset({
        "music.163.com",
        "y.music.163.com",
        "y.qq.com",
        "i.y.qq.com",
        "c6.y.qq.com",
    })

    @staticmethod
    def _is_allowed_redirect(url: str) -> bool:
        """检查重定向目标 URL 是否在允许的域名白名单内。

        Args:
            url: 重定向目标 URL。

        Returns:
            目标 host 在白名单内返回 True。
        """
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            return False
        return host in MusicSearchClient._ALLOWED_REDIRECT_HOSTS

    async def resolve_short_url(self, url: str) -> str | None:
        """解析音乐短链接，获取重定向后的完整 URL。

        对于 HTTP 302 重定向（如 c6.y.qq.com），只读取 Location header，
        不下载页面内容。对于 JS 重定向（如 163cn.tv），需要下载 HTML 解析。
        重定向目标仅允许 music.163.com / y.qq.com 等已知音乐服务域名，
        防止短链被利用指向内网地址（SSRF）。

        Args:
            url: 短链接地址。

        Returns:
            重定向后的最终 URL，失败或目标不在白名单内返回 None。
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
                if location and self._is_allowed_redirect(location):
                    logger.debug("短链接HTTP重定向: %s → %s", url, location)
                    return location
                if location:
                    logger.warning("短链接重定向目标不在白名单内: %s → %s", url, location)
        except Exception:
            logger.debug("短链接HTTP重定向解析失败: %s", url)

        # HTTP 重定向未获取到，尝试跟随重定向后从最终 URL 或 HTML 中提取
        try:
            resp = await client.get(url, follow_redirects=True)
            final_url = str(resp.url)

            # 如果最终 URL 在白名单内，重定向成功
            if self._is_allowed_redirect(final_url):
                return final_url

            # 尝试从 HTML 内容中提取跳转 URL（163cn.tv JS 重定向的情况）
            html = resp.text
            meta_match = re.search(
                r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\']?\d+;\s*url=([^"\'>\s]+)',
                html,
                re.IGNORECASE,
            )
            if meta_match:
                meta_url = meta_match.group(1).strip()
                if self._is_allowed_redirect(meta_url):
                    return meta_url
                logger.warning("短链接meta重定向目标不在白名单内: %s → %s", url, meta_url)
                return None
            js_match = re.search(
                r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                html,
            )
            if js_match:
                js_url = js_match.group(1).strip()
                if self._is_allowed_redirect(js_url):
                    return js_url
                logger.warning("短链接JS重定向目标不在白名单内: %s → %s", url, js_url)
                return None

            # 最终 URL 不在白名单内
            logger.warning("短链接最终目标不在白名单内: %s → %s", url, final_url)
            return None
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
