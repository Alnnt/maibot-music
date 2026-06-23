"""音乐插件 — 搜索点歌、解析音乐链接，发送语音音频。"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .music_api import MusicSearchClient, SongInfo
from .url_parser import extract_urls, parse_music_card_text, parse_music_url


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class MusicConfig(PluginConfigBase):
    """音乐配置。"""

    __ui_label__ = "音乐"
    __ui_icon__ = "music"
    __ui_order__ = 1

    default_platform: str = Field(
        default="163",
        description="默认音乐平台: 163(网易云) 或 qq(QQ音乐)",
    )
    auto_parse_url: bool = Field(default=True, description="是否自动解析音乐链接")
    auto_parse_card: bool = Field(default=True, description="是否自动解析音乐卡片")
    search_limit: int = Field(default=5, description="搜索结果数量")


class NeteaseConfig(PluginConfigBase):
    """网易云音乐配置。"""

    __ui_label__ = "网易云音乐"
    __ui_icon__ = "cloud"
    __ui_order__ = 2

    MUSIC_U: str = Field(
        default="",
        description="MUSIC_U — 登录凭证，用于获取高音质",
    )
    csrf_token: str = Field(
        default="",
        description="__csrf — CSRF 令牌，与 MUSIC_U 配对",
    )


class QQMusicConfig(PluginConfigBase):
    """QQ音乐配置。"""

    __ui_label__ = "QQ音乐"
    __ui_icon__ = "headphones"
    __ui_order__ = 3

    uin: str = Field(
        default="",
        description="uin — QQ 号",
    )
    qqmusic_key: str = Field(
        default="",
        description="qqmusic_key — 鉴权令牌，VIP 用户用于获取高音质",
    )


class NapCatConfig(PluginConfigBase):
    """NapCat HTTP API 配置（用于解析音乐卡片原始数据）。"""

    __ui_label__ = "NapCat"
    __ui_icon__ = "server"
    __ui_order__ = 4

    http_url: str = Field(
        default="http://127.0.0.1:3000",
        description="NapCat HTTP API 地址（如 http://127.0.0.1:3000）",
    )
    http_token: str = Field(
        default="",
        description="NapCat HTTP API 访问令牌（留空则不鉴权）",
    )


class MusicPluginConfig(PluginConfigBase):
    """音乐插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    netease: NeteaseConfig = Field(default_factory=NeteaseConfig)
    qq: QQMusicConfig = Field(default_factory=QQMusicConfig)
    napcat: NapCatConfig = Field(default_factory=NapCatConfig)


# ===== 待选状态 =====

# key: stream_id, value: (结果列表, 平台)
_pending_choices: dict[str, tuple[list[SongInfo], str]] = {}


# ===== 插件主类 =====


class MusicPlugin(MaiBotPlugin):
    """音乐插件 — 搜索点歌、解析音乐链接，发送语音音频。"""

    config_model = MusicPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._api: MusicSearchClient | None = None

    def _get_api(self) -> MusicSearchClient:
        """获取或创建 API 客户端。"""
        if self._api is None:
            netease_cookie: dict[str, str] = {}
            if self.config.netease.MUSIC_U:
                netease_cookie["MUSIC_U"] = self.config.netease.MUSIC_U
            if self.config.netease.csrf_token:
                netease_cookie["__csrf"] = self.config.netease.csrf_token

            qq_cookie: dict[str, str] = {}
            if self.config.qq.uin:
                qq_cookie["uin"] = self.config.qq.uin
            if self.config.qq.qqmusic_key:
                qq_cookie["qqmusic_key"] = self.config.qq.qqmusic_key

            self._api = MusicSearchClient(
                netease_cookie=netease_cookie,
                qq_cookie=qq_cookie,
            )
        return self._api

    def _resolve_platform(self, platform: str = "") -> str:
        """解析音乐平台，优先使用传入值，否则使用配置默认值。

        Args:
            platform: 传入的平台标识。

        Returns:
            有效的平台标识 ("163" 或 "qq")。
        """
        p = platform.strip().lower()
        if p in ("163", "qq"):
            return p
        if p in ("网易", "netease", "网易云音乐"):
            return "163"
        if p in ("qq", "qq音乐", "qqmusic"):
            return "qq"
        default = self.config.music.default_platform.strip().lower()
        return default if default in ("163", "qq") else "163"

    def _format_results(self, results: list[SongInfo]) -> str:
        """将搜索结果格式化为供用户选择的文本。

        Args:
            results: 搜索结果列表。

        Returns:
            格式化的选择文本。
        """
        lines = ["🎵 搜索结果："]
        for i, song in enumerate(results, 1):
            artist_part = f" - {song.artists}" if song.artists else ""
            lines.append(f"  {i}. {song.name}{artist_part}")
        lines.append("使用 /选歌 <序号> 选择歌曲，如 /选歌 1")
        return "\n".join(lines)

    async def _send_song(self, song: SongInfo, stream_id: str) -> None:
        """发送歌曲语音音频。

        Args:
            song: SongInfo 对象。
            stream_id: 目标消息流 ID。
        """
        api = self._get_api()

        # QQ 音乐专辑曲目的 songmid 和 strMediaMid 通常不同，
        # 如果 media_id 为空则通过详情接口补查，避免构造错误的播放 filename
        media_id = song.media_id
        if song.platform == "qq" and not media_id:
            try:
                detail = await api.get_qq_song_detail(song.song_id)
                if detail:
                    media_id = detail.media_id
            except Exception:
                self.ctx.logger.debug("QQ音乐详情查询失败: %s", song.song_id)

        try:
            audio_url = await api.get_song_url(song.song_id, song.platform, media_id)
        except Exception:
            self.ctx.logger.exception("获取音频URL异常: %s", song.song_id)
            return

        if not audio_url:
            self.ctx.logger.info("未获取到音频URL: %s %s", song.platform, song.song_id)
            await self.ctx.send.text(
                f"找到「{song.display()}」但无法获取音频，可能因版权限制",
                stream_id,
            )
            return

        try:
            await self.ctx.send.custom(
                "voiceurl",
                {"url": audio_url},
                stream_id,
            )
        except Exception:
            self.ctx.logger.exception("发送语音音频失败: %s", audio_url)
            await self.ctx.send.text(song.display(), stream_id)

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """插件加载。"""
        self.ctx.logger.info("音乐插件已加载")

    async def on_unload(self) -> None:
        """插件卸载，关闭 HTTP 客户端。"""
        if self._api is not None:
            await self._api.close()
            self._api = None
        _pending_choices.clear()
        self.ctx.logger.info("音乐插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载 — 重置 API 客户端以应用新 Cookie。"""
        if self._api is not None:
            await self._api.close()
            self._api = None
        self.ctx.logger.info("音乐插件配置已更新，API 客户端已重置")

    # ===== Tool 组件 =====

    @Tool(
        "search_and_play_music",
        description="搜索歌曲并发送语音音频。当用户想听歌、点歌、搜歌、找歌时使用此工具。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="歌曲名或关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="platform",
                param_type=ToolParamType.STRING,
                description="音乐平台: 163(网易云) 或 qq(QQ音乐)，不填使用默认平台",
                required=False,
            ),
        ],
    )
    async def handle_search_music(
        self,
        query: str = "",
        platform: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """搜索歌曲并发送。"""
        del kwargs

        if not query.strip():
            return {"content": "请提供歌曲名或关键词"}

        resolved_platform = self._resolve_platform(platform)
        api = self._get_api()

        try:
            results = await api.search(query, resolved_platform, limit=self.config.music.search_limit)
        except Exception:
            self.ctx.logger.exception("音乐搜索异常: %s", query)
            return {"content": "搜索歌曲时出错，请稍后再试"}

        if not results:
            platform_name = "网易云音乐" if resolved_platform == "163" else "QQ音乐"
            return {"content": f"在{platform_name}上未找到「{query}」相关歌曲"}

        # 只有一首结果时直接发送
        if len(results) == 1:
            await self._send_song(results[0], stream_id)
            return {"content": f"已发送: {results[0].display()}"}

        # 多首结果时列出选择
        _pending_choices[stream_id] = (results, resolved_platform)
        text = self._format_results(results)
        await self.ctx.send.text(text, stream_id)
        return {"content": f"找到{len(results)}首歌曲，已列出供用户选择"}

    # ===== Command 组件 =====

    @Command(
        "点歌",
        description="点歌命令，搜索歌曲并列出选择",
        pattern=r"^/点歌(?:\s+(163|qq|网易|qq音乐))?\s+(.+)$",
    )
    async def handle_music_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /点歌 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        platform_hint = str(matched_groups.get("1", "") or kwargs.get("1", "") or "").strip()
        query = str(matched_groups.get("2", "") or "").strip()

        # 如果 matched_groups 没有分组信息，尝试从原始消息解析
        if not query:
            raw_text = str(kwargs.get("text", "") or kwargs.get("message", "") or "")
            match = re.match(r"^/点歌(?:\s+(163|qq|网易|qq音乐))?\s+(.+)$", raw_text, re.DOTALL)
            if match:
                platform_hint = platform_hint or (match.group(1) or "")
                query = match.group(2) or ""

        if not query:
            await self.ctx.send.text("用法：/点歌 [163|qq] <歌曲名>", stream_id)
            return False, "缺少歌曲名", True

        resolved_platform = self._resolve_platform(platform_hint)
        api = self._get_api()

        try:
            results = await api.search(query, resolved_platform, limit=self.config.music.search_limit)
        except Exception:
            self.ctx.logger.exception("音乐搜索异常: %s", query)
            await self.ctx.send.text("搜索歌曲时出错，请稍后再试", stream_id)
            return False, "搜索异常", True

        if not results:
            platform_name = "网易云音乐" if resolved_platform == "163" else "QQ音乐"
            await self.ctx.send.text(f"在{platform_name}上未找到「{query}」相关歌曲", stream_id)
            return False, "未找到歌曲", True

        # 只有一首结果时直接发送
        if len(results) == 1:
            song = results[0]
            await self._send_song(song, stream_id)
            return True, f"已点歌: {song.display()}", True

        # 多首结果时列出选择
        _pending_choices[stream_id] = (results, resolved_platform)
        text = self._format_results(results)
        await self.ctx.send.text(text, stream_id)
        return True, f"列出{len(results)}首歌曲供选择", True

    @Command(
        "选歌",
        description="选择搜索结果中的歌曲",
        pattern=r"^/选歌\s+(\d+)$",
    )
    async def handle_select_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /选歌 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        index_str = str(matched_groups.get("1", "") or "").strip()

        if not index_str:
            raw_text = str(kwargs.get("text", "") or kwargs.get("message", "") or "")
            match = re.match(r"^/选歌\s+(\d+)$", raw_text)
            if match:
                index_str = match.group(1)

        if not index_str:
            await self.ctx.send.text("用法：/选歌 <序号>", stream_id)
            return False, "缺少序号", True

        # 查找待选状态
        pending = _pending_choices.pop(stream_id, None)
        if pending is None:
            await self.ctx.send.text("没有待选的歌曲，请先使用 /点歌 搜索", stream_id)
            return False, "无待选歌曲", True

        results, _platform = pending

        try:
            index = int(index_str)
        except ValueError:
            await self.ctx.send.text("请输入有效的数字序号", stream_id)
            return False, "序号无效", True

        if index < 1 or index > len(results):
            await self.ctx.send.text(f"序号超出范围，请输入 1~{len(results)} 之间的数字", stream_id)
            # 放回待选状态
            _pending_choices[stream_id] = pending
            return False, "序号超出范围", True

        song = results[index - 1]
        await self._send_song(song, stream_id)
        return True, f"已选择: {song.display()}", True

    # ===== EventHandler 组件 =====

    async def _resolve_music_card_from_raw(
        self,
        message: dict[str, Any],
    ) -> tuple[str, str] | None:
        """通过 NapCat HTTP API 获取原始消息，从 json 段解析音乐卡片的 jumpUrl。

        适配器将音乐卡片转成纯文本后会丢失歌曲 ID 等结构化数据。
        此方法直接调 NapCat 的 get_msg HTTP API 获取原始消息，
        从 json 段中提取 jumpUrl，精确解析出 (platform, song_id)。

        Args:
            message: MessageDict 对象。

        Returns:
            (platform, song_id) 元组，解析失败返回 None。
        """
        message_id = str(message.get("message_id", "")).strip()
        if not message_id:
            return None

        base_url = self.config.napcat.http_url.strip().rstrip("/")
        if not base_url:
            return None

        try:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            token = self.config.napcat.http_token.strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{base_url}/get_msg",
                    json={"message_id": int(message_id)},
                    headers=headers,
                )
                resp.raise_for_status()
                raw_detail = resp.json()
        except Exception:
            self.ctx.logger.debug("调用 NapCat get_msg 失败: %s", message_id)
            return None

        # 从响应中提取 data
        data = raw_detail.get("data") if isinstance(raw_detail, dict) else None
        if not isinstance(data, dict):
            return None

        # 原始消息中的 message 段列表
        raw_segments = data.get("message", [])
        if not isinstance(raw_segments, list):
            return None

        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != "json":
                continue

            segment_data = segment.get("data", {})
            json_str = str(segment_data.get("data") or "").strip() if isinstance(segment_data, dict) else ""
            if not json_str:
                continue

            try:
                parsed = json.loads(json_str)
            except Exception:
                continue

            if not isinstance(parsed, dict):
                continue

            app_name = str(parsed.get("app") or "").strip()
            meta = parsed.get("meta", {})
            if not isinstance(meta, dict):
                continue

            # 音乐卡片 — com.tencent.music.lua / com.tencent.structmsg
            if app_name in {"com.tencent.music.lua", "com.tencent.structmsg"}:
                # 优先 meta.music，其次 meta.news
                music_meta = meta.get("music", {})
                if not isinstance(music_meta, dict) or not music_meta:
                    music_meta = meta.get("news", {})
                if isinstance(music_meta, dict) and music_meta:
                    jump_url = str(music_meta.get("jumpUrl") or "").strip()
                    if jump_url:
                        # 网易云短链需要先解析重定向
                        if "163cn.tv" in jump_url:
                            api = self._get_api()
                            resolved_url = await api.resolve_short_url(jump_url)
                            if resolved_url:
                                jump_url = resolved_url
                        result = parse_music_url(jump_url)
                        if result:
                            return result

            # 音乐小程序 — com.tencent.miniapp_01
            if app_name == "com.tencent.miniapp_01":
                detail = meta.get("detail_1", {})
                if isinstance(detail, dict):
                    qqdocurl = str(detail.get("qqdocurl") or "").strip()
                    miniapp_title = str(detail.get("title") or "").strip()
                    if qqdocurl and miniapp_title in ("QQ音乐", "网易云音乐"):
                        if "163cn.tv" in qqdocurl:
                            api = self._get_api()
                            resolved_url = await api.resolve_short_url(qqdocurl)
                            if resolved_url:
                                qqdocurl = resolved_url
                        result = parse_music_url(qqdocurl)
                        if result:
                            return result

        return None

    @HookHandler(
        "chat.receive.after_process",
        name="music_url_parser",
        description="解析音乐链接和音乐卡片，发送语音音频",
        mode=HookMode.BLOCKING,
        order="normal",
    )
    async def handle_music_url_parse(self, **kwargs: Any) -> dict[str, Any]:
        """解析消息中的音乐链接和音乐分享卡片，发送音乐卡片和语音。

        Returns:
            dict: Hook 返回值。aborted=True 时阻止消息进入聊天流程。
        """
        message = kwargs.get("message")
        if not message:
            return {"action": "continue"}

        # 提取消息文本和 stream_id
        text = ""
        stream_id = ""
        message_id = ""

        if isinstance(message, dict):
            text = (
                message.get("processed_plain_text", "")
                or message.get("plain_text", "")
                or message.get("display_message", "")
                or ""
            )
            stream_id = str(message.get("stream_id", "") or message.get("session_id", "") or "")
            message_id = str(message.get("message_id", "") or "")
            if not text:
                raw_msg = message.get("raw_message", [])
                if isinstance(raw_msg, list):
                    text = " ".join(
                        str(seg.get("data", "")) if isinstance(seg, dict) and seg.get("type") == "text" else ""
                        for seg in raw_msg
                    ).strip()
        else:
            text = str(message)

        if not text or not stream_id:
            return {"action": "continue"}

        # ── 1. 音乐卡片解析 ──
        if self.config.music.auto_parse_card:
            card_info = parse_music_card_text(text)
            if card_info and card_info.query:
                # 优先通过 get_msg API 从原始消息中精确解析歌曲 ID
                card_result = None
                if isinstance(message, dict) and message_id:
                    card_result = await self._resolve_music_card_from_raw(message)

                # 其次从分享文本中的 URL 精确解析歌曲 ID
                if not card_result and card_info.url:
                    url_result = parse_music_url(card_info.url)
                    if url_result:
                        platform, song_id = url_result
                        # 网易云短链接需要重定向解析
                        if platform == "163_short":
                            api = self._get_api()
                            resolved_url = await api.resolve_short_url(card_info.url)
                            if resolved_url:
                                short_result = parse_music_url(resolved_url)
                                if short_result and short_result[0] != "163_short":
                                    platform, song_id = short_result
                                    card_result = (platform, song_id)
                        else:
                            card_result = url_result

                if card_result:
                    platform, song_id = card_result
                    await self._send_song(
                        SongInfo(
                            song_id=song_id,
                            name=card_info.song_name,
                            artists=card_info.artist,
                            album="",
                            platform=platform,
                        ),
                        stream_id,
                    )
                    self.ctx.logger.info(
                        "已解析音乐卡片(精确): %s → %s %s",
                        card_info.query,
                        platform,
                        song_id,
                    )
                    return {"action": "abort"}

                # 精确解析失败，检查文本中是否有音乐 URL 可供步骤2处理
                urls_in_text = extract_urls(text)
                has_music_link = any(
                    parse_music_url(u) is not None for u in urls_in_text
                )
                if has_music_link and self.config.music.auto_parse_url:
                    # 有音乐链接且 URL 解析已开启，跳到步骤2处理
                    pass
                else:
                    # 无音乐链接或 URL 解析已关闭，用歌名+歌手搜索
                    platform = card_info.platform or self._resolve_platform("")
                    api = self._get_api()
                    try:
                        results = await api.search(card_info.query, platform, limit=1)
                    except Exception:
                        self.ctx.logger.exception("音乐卡片搜索异常: %s", card_info.query)
                        results = []

                    if results:
                        await self._send_song(results[0], stream_id)
                        self.ctx.logger.info(
                            "已解析音乐卡片(搜索): %s → %s",
                            card_info.query,
                            results[0].display(),
                        )
                        return {"action": "abort"}
                    else:
                        self.ctx.logger.info("音乐卡片搜索无结果: %s", card_info.query)
                        return {"action": "continue"}

        # ── 2. URL 解析 ──
        if not self.config.music.auto_parse_url:
            return {"action": "continue"}

        # 查找文本中的 URL
        urls = extract_urls(text)
        if not urls:
            return {"action": "continue"}

        # 尝试解析每个 URL
        for url in urls:
            result = parse_music_url(url)
            if result is None:
                continue

            platform, song_id = result

            # 处理网易云短链接（163cn.tv）— 需重定向解析
            if platform == "163_short":
                api = self._get_api()
                resolved_url = await api.resolve_short_url(url)
                if resolved_url:
                    short_result = parse_music_url(resolved_url)
                    if short_result and short_result[0] != "163_short":
                        platform, song_id = short_result
                    else:
                        continue
                else:
                    continue

            # QQ 音乐需要 strMediaMid 构造正确的播放 filename
            # URL 只能解析出 song_mid，通过详情接口补查 media_id
            media_id = ""
            if platform == "qq":
                api = self._get_api()
                try:
                    detail = await api.get_qq_song_detail(song_id)
                    if detail:
                        media_id = detail.media_id
                except Exception:
                    self.ctx.logger.debug("QQ音乐详情查询失败: %s", song_id)

            # 发送语音音频
            api = self._get_api()
            try:
                audio_url = await api.get_song_url(song_id, platform, media_id)
                if audio_url:
                    await self.ctx.send.custom(
                        "voiceurl",
                        {"url": audio_url},
                        stream_id,
                    )
                    self.ctx.logger.info("已解析音乐链接: %s %s", platform, song_id)
                else:
                    self.ctx.logger.info("未获取到音频URL: %s %s", platform, song_id)
            except Exception:
                self.ctx.logger.exception("发送语音音频失败: %s %s", platform, song_id)

            # 只处理第一个匹配的音乐链接，拦截消息
            return {"action": "abort"}

        # 未匹配到任何音乐链接/卡片，不拦截
        return {"action": "continue"}


def create_plugin() -> MusicPlugin:
    """创建音乐插件实例。"""
    return MusicPlugin()
