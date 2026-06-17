"""音乐 URL 解析器 — 从文本中识别并提取音乐链接的歌曲 ID。"""

from __future__ import annotations

import re
from typing import Any


def parse_music_url(text: str) -> tuple[str, str] | None:
    """从文本中解析音乐 URL，提取平台和歌曲 ID。

    支持的 URL 格式：
        - 网易云: music.163.com/#/song?id=xxx
        - 网易云: music.163.com/song?id=xxx
        - 网易云: music.163.com/m/song?id=xxx
        - QQ音乐: y.qq.com/n/ryqq/songDetail/xxx
        - QQ音乐: y.qq.com/n/m/detail/song/xxx

    Args:
        text: 包含 URL 的文本。

    Returns:
        (platform, song_id) 元组，未匹配返回 None。
    """
    if not text:
        return None

    # 网易云音乐 — 标准 song?id= 格式
    netease_match = re.search(
        r"music\.163\.com/(?:#/)?(?:m/)?song\?id=(\d+)",
        text,
    )
    if netease_match:
        return ("163", netease_match.group(1))

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
