"""音乐插件 — 搜索点歌、解析音乐链接，发送音乐卡片和/或语音音频。"""

from __future__ import annotations

import re
from typing import Any

from maibot_sdk import Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import EventType, ToolParameterInfo, ToolParamType

from .music_api import MusicSearchClient, SongInfo
from .url_parser import extract_urls, parse_music_url


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
    send_mode: str = Field(
        default="card",
        description="发送方式: card(仅音乐卡片)、voice(仅语音音频)、both(两者都发)",
    )


class MusicPluginConfig(PluginConfigBase):
    """音乐插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)


# ===== 待选状态 =====

# key: stream_id, value: (结果列表, 平台)
_pending_choices: dict[str, tuple[list[SongInfo], str]] = {}


# ===== 插件主类 =====


class MusicPlugin(MaiBotPlugin):
    """音乐插件 — 搜索点歌、解析音乐链接，发送音乐卡片和/或语音音频。"""

    config_model = MusicPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._api: MusicSearchClient | None = None

    def _get_api(self) -> MusicSearchClient:
        """获取或创建 API 客户端。"""
        if self._api is None:
            self._api = MusicSearchClient()
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

    def _resolve_send_mode(self) -> str:
        """解析发送模式配置。

        Returns:
            "card"、"voice" 或 "both"。
        """
        mode = self.config.music.send_mode.strip().lower()
        if mode in ("card", "voice", "both"):
            return mode
        return "card"

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
        """根据配置的发送模式发送歌曲。

        Args:
            song: SongInfo 对象。
            stream_id: 目标消息流 ID。
        """
        mode = self._resolve_send_mode()

        # 发送音乐卡片
        if mode in ("card", "both"):
            try:
                await self.ctx.send.custom(
                    "music",
                    {"type": song.platform, "id": song.song_id},
                    stream_id,
                )
            except Exception:
                self.ctx.logger.exception("发送音乐卡片失败: %s", song.song_id)

        # 发送语音音频
        if mode in ("voice", "both"):
            api = self._get_api()
            try:
                audio_url = await api.get_song_url(song.song_id, song.platform)
            except Exception:
                self.ctx.logger.exception("获取音频URL异常: %s", song.song_id)
                return

            if not audio_url:
                self.ctx.logger.info("未获取到音频URL: %s %s", song.platform, song.song_id)
                if mode == "voice":
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
                if mode == "voice":
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
        """配置热重载。"""

    # ===== Tool 组件 =====

    @Tool(
        "search_and_play_music",
        description="搜索歌曲并发送音乐卡片和语音。当用户想听歌、点歌、搜歌、找歌时使用此工具。",
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

    @EventHandler(
        "music_url_parser",
        description="解析音乐链接，发送音乐卡片和语音",
        event_type=EventType.ON_MESSAGE,
    )
    async def handle_music_url_parse(
        self,
        message: Any = None,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, bool, str | None, None, None]:
        """解析消息中的音乐链接，发送音乐卡片和语音。"""
        del kwargs

        if not self.config.music.auto_parse_url or not message:
            return True, True, None, None, None

        # 提取消息文本
        text = ""
        if isinstance(message, dict):
            text = message.get("plain_text", "") or message.get("raw_message", "") or ""
        else:
            text = str(message)

        if not text:
            return True, True, None, None, None

        # 查找文本中的 URL
        urls = extract_urls(text)
        if not urls:
            return True, True, None, None, None

        # 尝试解析每个 URL
        for url in urls:
            result = parse_music_url(url)
            if result is None:
                continue

            platform, song_id = result

            # 处理网易云短链接（163cn.tv）
            if "163cn.tv" in url:
                api = self._get_api()
                resolved_url = await api.resolve_short_url(url)
                if resolved_url:
                    short_result = parse_music_url(resolved_url)
                    if short_result:
                        platform, song_id = short_result

            # 根据配置发送
            mode = self._resolve_send_mode()

            # 发送音乐卡片
            if mode in ("card", "both"):
                try:
                    await self.ctx.send.custom(
                        "music",
                        {"type": platform, "id": song_id},
                        stream_id,
                    )
                    self.ctx.logger.info("已解析音乐链接: %s %s", platform, song_id)
                except Exception:
                    self.ctx.logger.exception("发送音乐卡片失败: %s %s", platform, song_id)

            # 发送语音音频
            if mode in ("voice", "both"):
                api = self._get_api()
                try:
                    audio_url = await api.get_song_url(song_id, platform)
                    if audio_url:
                        await self.ctx.send.custom(
                            "voiceurl",
                            {"url": audio_url},
                            stream_id,
                        )
                except Exception:
                    self.ctx.logger.exception("发送语音音频失败: %s %s", platform, song_id)

            # 只处理第一个匹配的音乐链接
            break

        return True, True, None, None, None


def create_plugin() -> MusicPlugin:
    """创建音乐插件实例。"""
    return MusicPlugin()
