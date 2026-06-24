"""音乐 URL 解析器 — 从文本中识别并提取音乐链接的歌曲 ID。"""

from __future__ import annotations

import re


def parse_music_url(text: str) -> tuple[str, str] | None:
    """从文本中解析音乐 URL，提取平台和歌曲 ID。

    支持的 URL 格式：
        - 网易云: music.163.com/#/song?id=xxx
        - 网易云: music.163.com/song?id=xxx
        - 网易云: music.163.com/m/song?id=xxx
        - 网易云: y.music.163.com/m/song?id=xxx（卡片 jumpUrl）
        - 网易云: 163cn.tv/xxx（短链接，需重定向解析）
        - QQ音乐: y.qq.com/n/ryqq/songDetail/xxx
        - QQ音乐: y.qq.com/n/m/detail/song/xxx
        - QQ音乐: i.y.qq.com/v8/playsong.html?songmid=xxx（卡片 jumpUrl）

    Args:
        text: 包含 URL 的文本。

    Returns:
        (platform, song_id) 元组，未匹配返回 None。
        对于网易云短链接，返回 ("163_short", 短链ID)，需要调用方做重定向解析。
    """
    if not text:
        return None

    # 网易云音乐 — 标准 song?id= 格式（含 y.music.163.com 卡片 jumpUrl）
    netease_match = re.search(
        r"(?:y\.)?music\.163\.com/(?:#/)?(?:m/)?song\?id=(\d+)",
        text,
    )
    if netease_match:
        return ("163", netease_match.group(1))

    # 网易云音乐短链接 — 163cn.tv/xxx（需重定向解析）
    netease_short_match = re.search(
        r"163cn\.tv/([A-Za-z0-9]+)",
        text,
    )
    if netease_short_match:
        return ("163_short", netease_short_match.group(1))

    # QQ音乐 — i.y.qq.com/v8/playsong.html?songmid=xxx（卡片 jumpUrl）
    qq_playsong_match = re.search(
        r"y\.qq\.com/v8/playsong\.html\?",
        text,
    )
    if qq_playsong_match:
        # 提取 songmid（必须用 songmid，不能回退到 media_mid，否则下游 filename 构造错误）
        songmid_match = re.search(r"[?&]songmid=([A-Za-z0-9]+)", text)
        if songmid_match and songmid_match.group(1):
            return ("qq", songmid_match.group(1))

    # QQ音乐 — songDetail/xxx 格式
    qq_match = re.search(
        r"y\.qq\.com/n/(?:ryqq/)?songDetail/([A-Za-z0-9]+)",
        text,
    )
    if qq_match:
        return ("qq", qq_match.group(1))

    # QQ音乐 — detail/song/xxx 格式
    qq_match2 = re.search(
        r"y\.qq\.com/n/m/detail/song/([A-Za-z0-9]+)",
        text,
    )
    if qq_match2:
        return ("qq", qq_match2.group(1))

    return None


def has_music_url(text: str) -> bool:
    """检查文本中是否包含音乐链接。

    Args:
        text: 待检查文本。

    Returns:
        是否包含音乐链接。
    """
    return parse_music_url(text) is not None


def extract_urls(text: str) -> list[str]:
    """从文本中提取所有 URL。

    Args:
        text: 待提取文本。

    Returns:
        URL 列表。
    """
    url_pattern = re.compile(
        r"https?://[^\s<>\"]+|www\.[^\s<>\"]+",
        re.IGNORECASE,
    )
    return url_pattern.findall(text)


# ===== 音乐卡片文本解析 =====

# 音乐分享卡片格式：[QQ音乐] 歌名 - 歌手  或  [网易云音乐] 歌名 - 歌手
# 歌名用贪婪匹配，以最后一个 " - " 作为分隔，避免歌名含横杠时截断
_MUSIC_CARD_PATTERN = re.compile(
    r"\[(QQ音乐|网易云音乐|音乐分享)\]\s*(.+)\s+-\s+(.+)$",
)

# 小程序分享格式：[小程序] QQ音乐：歌名 - 歌手  或  [小程序] 网易云音乐：歌名
_MINIAPP_MUSIC_PATTERN = re.compile(
    r"\[小程序\]\s*(QQ音乐|网易云音乐)\s*[：:]\s*(.+)\s+-\s+(.+)$",
)

# 网易云音乐分享格式：分享xxx的单曲《歌名》: URL (来自@网易云音乐)
# 歌名中可能含有》字符，需用贪婪匹配；URL 以空格/括号为边界
_NETEASE_SHARE_PATTERN = re.compile(
    r"分享.+?的单曲《(.+)》\s*[：:]\s*(https?://[^\s()]+)",
)

# QQ音乐分享格式：分享歌曲 《歌名》URL @QQ音乐
_QQ_SHARE_PATTERN = re.compile(
    r"分享歌曲\s*《(.+?)》\s*(https?://\S+)?\s*@?(QQ音乐|网易云音乐)?",
)

# 平台显示名 → 内部标识
_PLATFORM_TAG_MAP: dict[str, str] = {
    "QQ音乐": "qq",
    "网易云音乐": "163",
}


class MusicCardInfo:
    """从文本中解析出的音乐卡片信息。"""

    __slots__ = ("platform", "query", "song_name", "artist", "url")

    def __init__(
        self,
        platform: str,
        query: str,
        song_name: str = "",
        artist: str = "",
        url: str = "",
    ) -> None:
        self.platform = platform  # "163" 或 "qq"
        self.query = query  # 搜索关键词（歌名 或 歌名 歌手）
        self.song_name = song_name  # 歌曲名
        self.artist = artist  # 歌手名
        self.url = url  # 分享文本中的音乐链接（可能包含精确的歌曲 ID）


def parse_music_card_text(text: str) -> MusicCardInfo | None:
    """从适配器转换后的纯文本中识别音乐分享卡片。

    识别格式：
        - 音乐卡片：[QQ音乐] 小城夏天 - LBI利比
        - 音乐卡片：[网易云音乐] 我的悲伤是水做的 - ChiliChill
        - 小程序：[小程序] QQ音乐：小城夏天 - LBI利比

    Args:
        text: 消息纯文本。

    Returns:
        MusicCardInfo，未匹配返回 None。
    """
    if not text:
        return None

    # 1. 尝试匹配音乐分享卡片
    match = _MUSIC_CARD_PATTERN.match(text.strip())
    if match:
        tag = match.group(1)
        song_name = match.group(2).strip()
        artist = (match.group(3) or "").strip()
        platform = _PLATFORM_TAG_MAP.get(tag)
        if not platform:
            # "音乐分享" 无法确定平台，用空字符串标记
            platform = ""
        query = f"{song_name} {artist}".strip() if artist else song_name
        return MusicCardInfo(platform=platform, query=query, song_name=song_name, artist=artist)

    # 2. 尝试匹配小程序分享
    match = _MINIAPP_MUSIC_PATTERN.match(text.strip())
    if match:
        tag = match.group(1)
        song_name = match.group(2).strip()
        artist = (match.group(3) or "").strip()
        platform = _PLATFORM_TAG_MAP.get(tag, "")
        query = f"{song_name} {artist}".strip() if artist else song_name
        return MusicCardInfo(platform=platform, query=query, song_name=song_name, artist=artist)

    # 3. 尝试匹配"分享xxx的单曲《歌名》"格式（网易云分享）
    match = _NETEASE_SHARE_PATTERN.search(text.strip())
    if match:
        song_name = match.group(1).strip()
        url = match.group(2).strip()
        return MusicCardInfo(platform="163", query=song_name, song_name=song_name, artist="", url=url)

    # 4. 尝试匹配"分享歌曲"格式
    match = _QQ_SHARE_PATTERN.search(text.strip())
    if match:
        song_name = match.group(1).strip()
        url = (match.group(2) or "").strip()
        tag = match.group(3) or ""
        platform = _PLATFORM_TAG_MAP.get(tag, "")
        return MusicCardInfo(platform=platform, query=song_name, song_name=song_name, artist="", url=url)

    return None
