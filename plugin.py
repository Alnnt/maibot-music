"""音乐插件 — 搜索点歌、解析音乐链接，发送语音音频。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Literal

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .audio_cache import AudioCacheError, MusicAudioCache
from .music_api import MusicAPIResponseError, MusicSearchClient, SongInfo
from .url_parser import extract_urls, parse_music_card_text, parse_music_url


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.4.2", description="配置版本")


class MusicConfig(PluginConfigBase):
    """音乐配置。"""

    __ui_label__ = "音乐"
    __ui_icon__ = "music"
    __ui_order__ = 1

    default_platform: str = Field(
        default="163",
        description="默认音乐平台: 163(网易云) 或 qq(QQ音乐)",
    )
    command_prefix: str = Field(default="/", description="命令前缀符号，如 / 或 #")
    auto_parse_url: bool = Field(default=True, description="是否自动解析音乐链接")
    auto_parse_card: bool = Field(default=True, description="是否自动解析音乐卡片")
    search_limit: int = Field(default=5, description="搜索结果数量")
    auto_select_first: bool = Field(
        default=False,
        description="搜索到多首歌曲时是否跳过选歌阶段，直接发送第一首",
    )
    play_mode: Literal["voice", "card"] = Field(
        default="card",
        description="播放模式: voice(语音音频) 或 card(音乐卡片)",
    )
    voice_source: Literal["local", "remote"] = Field(
        default="local",
        description="语音音频来源: local(本地缓存) 或 remote(远程URL)",
    )
    cache_storage_dir: str = Field(
        default="/root/maimai/MaiBot/data/music_cache",
        description="MaiBot 保存音乐缓存的目录",
    )
    cache_napcat_dir: str = Field(
        default="/app/music_cache",
        description="NapCat 读取音乐缓存的目录",
    )
    cache_max_size_mb: int = Field(default=1024, gt=0, description="音乐缓存最大容量（MB）")
    cache_expire_hours: int = Field(default=24, gt=0, description="超过此小时数未访问的缓存将被清理")
    cache_cleanup_interval_hours: int = Field(default=24, gt=0, description="缓存清理任务执行间隔（小时）")
    cache_max_file_size_mb: int = Field(default=50, gt=0, description="单个音乐缓存文件最大大小（MB）")
    cache_download_timeout_seconds: int = Field(default=30, gt=0, description="下载音乐文件超时时间（秒）")


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
        default="http://127.0.0.1:9999",
        description="NapCat HTTP API 地址（如 http://127.0.0.1:9999）",
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

# 待选状态过期时间（秒）
_PENDING_TTL = 300


# ===== 插件主类 =====


class MusicPlugin(MaiBotPlugin):
    """音乐插件 — 搜索点歌、解析音乐链接，发送语音音频。"""

    config_model = MusicPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._api: MusicSearchClient | None = None
        self._audio_cache: MusicAudioCache | None = None
        self._cache_cleanup_task: asyncio.Task[None] | None = None
        self._voice_send_condition = asyncio.Condition()
        self._active_voice_sends = 0
        self._stopping_audio_cache = False
        # key: stream_id（等于消息的 session_id）, value: (结果列表, 平台, 创建时间戳)
        self._pending_choices: dict[str, tuple[list[SongInfo], str, float]] = {}
        self._pending_lock = asyncio.Lock()

    def _clean_expired_pending(self) -> None:
        """清理已过期的待选状态。"""
        now = time.monotonic()
        expired = [sid for sid, (_, _, ts) in self._pending_choices.items() if now - ts > _PENDING_TTL]
        for sid in expired:
            del self._pending_choices[sid]

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
                napcat_url=self.config.napcat.http_url,
                napcat_token=self.config.napcat.http_token,
            )
        return self._api

    async def _start_audio_cache(self) -> None:
        """按当前配置初始化语音音频缓存。"""
        if self.config.music.play_mode != "voice" or self.config.music.voice_source != "local":
            async with self._voice_send_condition:
                self._stopping_audio_cache = False
                self._voice_send_condition.notify_all()
            return

        async with self._voice_send_condition:
            self._stopping_audio_cache = True

        audio_cache = MusicAudioCache(
            self.config.music.cache_storage_dir,
            self.config.music.cache_napcat_dir,
            max_size_bytes=self.config.music.cache_max_size_mb * 1024 * 1024,
            expire_seconds=self.config.music.cache_expire_hours * 3600,
            max_file_size_bytes=self.config.music.cache_max_file_size_mb * 1024 * 1024,
            download_timeout_seconds=self.config.music.cache_download_timeout_seconds,
        )
        try:
            await audio_cache.initialize()
        except BaseException:
            await audio_cache.close()
            raise

        self._audio_cache = audio_cache
        self._cache_cleanup_task = asyncio.create_task(
            self._cache_cleanup_loop(),
            name="maibot-music-cache-cleanup",
        )
        async with self._voice_send_condition:
            self._stopping_audio_cache = False
            self._voice_send_condition.notify_all()

    async def _stop_audio_cache(self) -> None:
        """等待语音发送结束后停止缓存任务和下载客户端。"""
        async with self._voice_send_condition:
            self._stopping_audio_cache = True
            await self._voice_send_condition.wait_for(lambda: self._active_voice_sends == 0)

        if self._cache_cleanup_task is not None:
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
            self._cache_cleanup_task = None

        if self._audio_cache is not None:
            await self._audio_cache.close()
            self._audio_cache = None

    async def _acquire_voice_send(self) -> tuple[Literal["local", "remote"], MusicAudioCache | None]:
        """注册一次语音发送并返回音频来源与缓存快照。"""
        async with self._voice_send_condition:
            await self._voice_send_condition.wait_for(lambda: not self._stopping_audio_cache)
            self._active_voice_sends += 1
            return self.config.music.voice_source, self._audio_cache

    async def _release_voice_send(self) -> None:
        """结束一次语音发送并唤醒等待中的缓存重载。"""
        async with self._voice_send_condition:
            self._active_voice_sends -= 1
            if self._active_voice_sends == 0:
                self._voice_send_condition.notify_all()

    async def _cache_cleanup_loop(self) -> None:
        """定期删除过期缓存并执行容量淘汰。"""
        interval_seconds = self.config.music.cache_cleanup_interval_hours * 3600
        while True:
            await asyncio.sleep(interval_seconds)
            if self._audio_cache is None:
                continue
            try:
                await self._audio_cache.cleanup()
            except Exception:
                self.ctx.logger.exception("清理音乐缓存失败")

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
        if p in ("qq音乐", "qqmusic"):
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
        pfx = self.config.music.command_prefix
        lines = ["🎵 搜索结果："]
        for i, song in enumerate(results, 1):
            artist_part = f" - {song.artists}" if song.artists else ""
            lines.append(f"  {i}. {song.name}{artist_part}")
        lines.append(f"使用 {pfx}选歌 <序号> 选择歌曲，如 {pfx}选歌 1")
        return "\n".join(lines)

    async def _send_music_card(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """以音乐卡片形式发送歌曲。

        通过 NapCat 平台型 music 段发送，只需 song_id 和 platform，
        NapCat 负责从音乐平台拉取音频和卡片展示信息。

        Args:
            song: SongInfo 对象。
            stream_id: 目标消息流 ID。
            silent: 是否静默处理失败（不向用户发送提示文本）。

        Returns:
            是否成功发送卡片。
        """
        try:
            sent = await self.ctx.send.custom(
                "music",
                {"type": song.platform, "id": song.song_id},
                stream_id,
            )
        except Exception:
            self.ctx.logger.exception("发送音乐卡片失败: %s %s", song.platform, song.song_id)
            if not silent:
                await self.ctx.send.text(song.display(), stream_id)
            return False

        if sent:
            return True

        self.ctx.logger.warning("音乐卡片发送失败: %s %s", song.platform, song.song_id)
        if not silent:
            await self.ctx.send.text(song.display(), stream_id)
        return False

    async def _send_voice_audio(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """获取歌曲音频并以语音消息发送。"""
        voice_source, audio_cache = await self._acquire_voice_send()
        cache_path = None
        try:
            api = self._get_api()

            # QQ 音乐专辑曲目的 songmid 和 strMediaMid 通常不同，
            # 如果 media_id 为空则通过详情接口补查，避免构造错误的播放 filename
            media_id = song.media_id
            if song.platform == "qq" and not media_id:
                detail = await api.get_qq_song_detail(song.song_id)
                if detail:
                    media_id = detail.media_id

            audio_url = await api.get_song_url(
                song.song_id,
                song.platform,
                media_id,
                mp3_only=voice_source == "local",
            )
            if not audio_url:
                self.ctx.logger.info("未获取到音频URL: %s %s", song.platform, song.song_id)
                if not silent:
                    await self.ctx.send.text(
                        f"找到「{song.display()}」但音乐平台未返回可用音频",
                        stream_id,
                    )
                return False

            voice_reference = audio_url
            if voice_source == "local":
                if audio_cache is None:
                    raise RuntimeError("本地音乐缓存尚未初始化")
                cache_path = await audio_cache.get_or_download(song.platform, song.song_id, audio_url)
                voice_reference = audio_cache.napcat_path(cache_path)

            sent = await self.ctx.send.custom(
                "voiceurl",
                {"url": voice_reference},
                stream_id,
            )
            if sent:
                return True

            self.ctx.logger.warning(
                "发送语音音频失败: platform=%s song_id=%s source=%s",
                song.platform,
                song.song_id,
                voice_source,
            )
            if not silent:
                await self.ctx.send.text(song.display(), stream_id)
            return False
        finally:
            if cache_path is not None and audio_cache is not None:
                await audio_cache.release(cache_path)
            await self._release_voice_send()

    async def _send_song(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """发送歌曲语音音频或音乐卡片，根据 play_mode 配置分发。"""
        if self.config.music.play_mode == "card":
            return await self._send_music_card(song, stream_id, silent=silent)

        try:
            return await self._send_voice_audio(song, stream_id, silent=silent)
        except AudioCacheError as exc:
            self.ctx.logger.warning("缓存音乐音频失败: %s %s: %s", song.platform, song.song_id, exc)
        except Exception:
            self.ctx.logger.exception("发送语音音频异常: %s %s", song.platform, song.song_id)

        if not silent:
            await self.ctx.send.text(f"「{song.display()}」的语音音频发送失败", stream_id)
        return False

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """插件加载，初始化本地音乐缓存。"""
        await self._start_audio_cache()
        self.ctx.logger.info("音乐插件已加载")

    async def on_unload(self) -> None:
        """插件卸载，关闭 HTTP 客户端和缓存任务。"""
        await self._stop_audio_cache()
        if self._api is not None:
            await self._api.close()
            self._api = None
        self._pending_choices.clear()
        self.ctx.logger.info("音乐插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载，重建 API 客户端和本地音乐缓存。"""
        del config_data, version
        if scope != "self":
            return

        await self._stop_audio_cache()
        if self._api is not None:
            await self._api.close()
            self._api = None
        await self._start_audio_cache()
        self.ctx.logger.info("音乐插件配置已更新，API 客户端和音乐缓存已重置")

    # ===== 搜索核心逻辑 =====

    async def _do_search_and_send(
        self,
        query: str,
        platform: str = "",
        stream_id: str = "",
    ) -> tuple[bool, str]:
        """搜索歌曲并发送，供 Tool 和 Command 共用。

        Args:
            query: 搜索关键词。
            platform: 平台标识（可为空，使用默认值）。
            stream_id: 目标消息流 ID。

        Returns:
            (成功与否, 描述文本) 元组。
        """
        resolved_platform = self._resolve_platform(platform)
        api = self._get_api()

        try:
            results = await api.search(query, resolved_platform, limit=self.config.music.search_limit)
        except MusicAPIResponseError as exc:
            self.ctx.logger.error("音乐搜索失败: %s", exc)
            await self.ctx.send.text("搜索歌曲时出错，请稍后再试", stream_id)
            return False, "搜索歌曲时出错，请稍后再试"
        except Exception:
            self.ctx.logger.exception("音乐搜索异常: %s", query)
            await self.ctx.send.text("搜索歌曲时出错，请稍后再试", stream_id)
            return False, "搜索歌曲时出错，请稍后再试"

        if not results:
            platform_name = "网易云音乐" if resolved_platform == "163" else "QQ音乐"
            msg = f"在{platform_name}上未找到「{query}」相关歌曲"
            await self.ctx.send.text(msg, stream_id)
            return False, msg

        # 只有一首结果时直接发送
        if len(results) == 1:
            sent = await self._send_song(results[0], stream_id)
            return sent, f"已发送: {results[0].display()}" if sent else f"发送失败: {results[0].display()}"

        # 多首结果时，根据配置决定是否跳过选歌阶段直接发送第一首
        if self.config.music.auto_select_first:
            sent = await self._send_song(results[0], stream_id)
            return sent, f"已发送: {results[0].display()}" if sent else f"发送失败: {results[0].display()}"

        # 多首结果时列出选择
        async with self._pending_lock:
            self._pending_choices[stream_id] = (results, resolved_platform, time.monotonic())
        text = self._format_results(results)
        await self.ctx.send.text(text, stream_id)
        return True, f"找到{len(results)}首歌曲，已列出供用户选择"

    # ===== Tool 组件 =====

    @Tool(
        "search_and_play_music",
        description=(
            "搜索歌曲并发送语音音频。当用户想听歌、点歌、搜歌、找歌时使用此工具。"
            "不要指定platform参数，让插件自动选择可用平台。"
            "本工具已内置重试和换源逻辑，如果返回播放失败，不要重复调用，直接告诉用户结果即可。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="歌曲名或关键词",
                required=True,
            ),
        ],
    )
    async def handle_search_music(
        self,
        query: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        """搜索歌曲并直接播放。

        与命令不同，Tool 调用时直接播放最佳匹配，不走"列出候选→用户选歌"流程。
        默认平台播放失败时自动换另一个平台重试，多首候选逐个尝试。
        """
        del kwargs
        _TOOL_NAME = "search_and_play_music"

        if not query.strip():
            return {"name": _TOOL_NAME, "content": "请提供歌曲名或关键词"}

        default = self._resolve_platform("")
        alt_platform = "163" if default == "qq" else "qq"
        ordered_platforms = [default, alt_platform]
        api = self._get_api()

        # 依次尝试：配置默认平台 → AI 指定平台 → 备选平台
        for try_platform in ordered_platforms:
            try:
                results = await api.search(query, try_platform, limit=self.config.music.search_limit)
            except MusicAPIResponseError as exc:
                self.ctx.logger.error("音乐搜索失败(%s): %s", try_platform, exc)
                continue
            except Exception:
                self.ctx.logger.exception("音乐搜索异常(%s): %s", try_platform, query)
                continue

            if not results:
                continue

            # 逐个尝试候选歌曲，直到成功播放一首
            for song in results:
                sent = await self._send_song(song, stream_id, silent=True)
                if sent:
                    return {"name": _TOOL_NAME, "content": f"已播放: {song.display()}"}

        return {
            "name": _TOOL_NAME,
            "content": (
                f"已尝试在网易云音乐和QQ音乐搜索「{query}」，音乐平台均未返回可用音频。"
                "请不要重复调用本工具，直接告知用户当前无法播放即可。"
            ),
        }

    # ===== Command 组件 =====

    @Command(
        "点歌",
        description="点歌命令，搜索歌曲并列出选择",
        pattern=r"^(?P<pfx>\S)点歌(?:\s+(?P<platform>163|qq|网易云音乐|网易|netease|qq音乐|qqmusic))?\s+(?P<query>.+)$",
    )
    async def handle_music_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理点歌命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        pfx = str(matched_groups.get("pfx", "") or "")
        configured_pfx = self.config.music.command_prefix
        if pfx and pfx != configured_pfx:
            return False, "", False

        platform_hint = str(matched_groups.get("platform", "") or "").strip()
        query = str(matched_groups.get("query", "") or "").strip()

        # 如果 matched_groups 没有分组信息，尝试从原始消息解析
        if not query:
            raw_text = str(kwargs.get("text", "") or kwargs.get("message", "") or "")
            epfx = re.escape(configured_pfx)
            match = re.match(
                rf"^{epfx}点歌(?:\s+(?P<platform>163|qq|网易云音乐|网易|netease|qq音乐|qqmusic))?\s+(?P<query>.+)$",
                raw_text,
                re.DOTALL,
            )
            if match:
                platform_hint = platform_hint or (match.group("platform") or "")
                query = match.group("query") or ""

        if not query:
            await self.ctx.send.text(f"用法：{configured_pfx}点歌 [163|qq] <歌曲名>", stream_id)
            return False, "缺少歌曲名", True

        success, message = await self._do_search_and_send(query, platform_hint, stream_id)
        return success, message, True

    @Command(
        "选歌",
        description="选择搜索结果中的歌曲",
        pattern=r"^(?P<pfx>\S)选歌\s+(?P<index>\d+)$",
    )
    async def handle_select_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理选歌命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        pfx = str(matched_groups.get("pfx", "") or "")
        configured_pfx = self.config.music.command_prefix
        if pfx and pfx != configured_pfx:
            return False, "", False

        index_str = str(matched_groups.get("index", "") or "").strip()

        if not index_str:
            raw_text = str(kwargs.get("text", "") or kwargs.get("message", "") or "")
            epfx = re.escape(configured_pfx)
            match = re.match(rf"^{epfx}选歌\s+(?P<index>\d+)$", raw_text)
            if match:
                index_str = match.group("index")

        if not index_str:
            await self.ctx.send.text(f"用法：{configured_pfx}选歌 <序号>", stream_id)
            return False, "缺少序号", True

        # 查找待选状态
        self._clean_expired_pending()
        async with self._pending_lock:
            pending = self._pending_choices.pop(stream_id, None)
            if pending is None:
                await self.ctx.send.text(f"没有待选的歌曲，请先使用 {configured_pfx}点歌 搜索", stream_id)
                return False, "无待选歌曲", True

            results, _platform, _ts = pending

            try:
                index = int(index_str)
            except ValueError:
                await self.ctx.send.text("请输入有效的数字序号", stream_id)
                # 放回待选状态
                self._pending_choices[stream_id] = pending
                return False, "序号无效", True

            if index < 1 or index > len(results):
                await self.ctx.send.text(f"序号超出范围，请输入 1~{len(results)} 之间的数字", stream_id)
                # 放回待选状态
                self._pending_choices[stream_id] = pending
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

        # message_id 必须是纯数字才能传给 NapCat API
        try:
            int_message_id = int(message_id)
        except ValueError:
            self.ctx.logger.debug("message_id 非数字，跳过原始消息解析: %s", message_id)
            return None

        api = self._get_api()
        data = await api.get_raw_message(int_message_id)
        if not data:
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
        timeout_ms=15000,
    )
    async def handle_music_url_parse(self, **kwargs: Any) -> dict[str, Any]:
        """解析消息中的音乐链接和音乐分享卡片，发送音乐卡片和语音。

        Returns:
            dict: Hook 返回值。aborted=True 时阻止消息进入聊天流程。
        """
        message = kwargs.get("message")
        if not message:
            return {"action": "continue"}

        # 提取消息文本和 session_id
        # SDK 保证 message 为 dict，非 dict 的情况不做处理
        if not isinstance(message, dict):
            return {"action": "continue"}

        text = message.get("processed_plain_text") or ""
        session_id = str(message.get("session_id", ""))
        message_id = str(message.get("message_id", ""))
        if not text:
            raw_msg = message.get("raw_message", [])
            if isinstance(raw_msg, list):
                text = " ".join(
                    str(seg.get("data", "")) if isinstance(seg, dict) and seg.get("type") == "text" else ""
                    for seg in raw_msg
                ).strip()

        if not text or not session_id:
            return {"action": "continue"}

        # ── 1. 音乐卡片解析 ──
        if self.config.music.auto_parse_card:
            card_info = parse_music_card_text(text)
            if card_info and card_info.query:
                # 优先通过 get_msg API 从原始消息中精确解析歌曲 ID
                card_result = None
                if message_id:
                    card_result = await self._resolve_music_card_from_raw(message)

                # 其次从分享文本中的 URL 精确解析歌曲 ID
                # card_info.url 来自 parse_music_card_text 对分享文本的提取，
                # 对于网易云短链接，card_info.url 就是短链接本身
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

                if card_result and card_result[0] not in ("163_short", "qq_short"):
                    platform, song_id = card_result
                    sent = await self._send_song(
                        SongInfo(
                            song_id=song_id,
                            name=card_info.song_name,
                            artists=card_info.artist,
                            album="",
                            platform=platform,
                        ),
                        session_id,
                    )
                    self.ctx.logger.info(
                        "已解析音乐卡片(精确): %s → %s %s",
                        card_info.query,
                        platform,
                        song_id,
                    )
                    return {"action": "abort" if sent else "continue"}

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
                    except MusicAPIResponseError as exc:
                        self.ctx.logger.error("音乐卡片搜索失败: %s", exc)
                        results = []
                    except Exception:
                        self.ctx.logger.exception("音乐卡片搜索异常: %s", card_info.query)
                        results = []

                    if results:
                        sent = await self._send_song(results[0], session_id)
                        self.ctx.logger.info(
                            "已解析音乐卡片(搜索): %s → %s",
                            card_info.query,
                            results[0].display(),
                        )
                        return {"action": "abort" if sent else "continue"}
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

            # 处理短链接（163cn.tv / c6.y.qq.com）— 需重定向解析
            if platform in ("163_short", "qq_short"):
                api = self._get_api()
                resolved_url = await api.resolve_short_url(url)
                if resolved_url:
                    short_result = parse_music_url(resolved_url)
                    if short_result and short_result[0] not in ("163_short", "qq_short"):
                        platform, song_id = short_result
                    else:
                        continue
                else:
                    continue

            # 发送语音音频（_send_song 内部会补查 QQ 音乐 media_id）
            sent = await self._send_song(
                SongInfo(
                    song_id=song_id,
                    name="",
                    artists="",
                    album="",
                    platform=platform,
                ),
                session_id,
            )
            self.ctx.logger.info("已解析音乐链接: %s %s", platform, song_id)

            # 只处理第一个匹配的音乐链接，发送成功时拦截消息
            return {"action": "abort" if sent else "continue"}

        # 未匹配到任何音乐链接/卡片，不拦截
        return {"action": "continue"}


def create_plugin() -> MusicPlugin:
    """创建音乐插件实例。"""
    return MusicPlugin()
