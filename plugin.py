# -*- coding: utf-8 -*-
"""B站视频解析与自动发送插件 -- SDK 2.0 版本。"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .downloader import download_video
from .ffmpeg import VideoCompressor, ffmpeg_manager
from .parser import BilibiliParser, BilibiliVideoInfo
from .sender import send_text, send_video
from .utils import ensure_shared_file_permissions, get_download_temp_dir


# ── 配置模型 ─────────────────────────────────────────────────


class BilibiliConfig(PluginConfigBase):
    """B站 API 与视频处理配置。"""
    __ui_label__ = "B站设置"
    __ui_icon__ = "videocam"
    __ui_order__ = 1

    sessdata: str = Field(default="", description="B 站登录 Cookie 中的 SESSDATA 值（用于获取高清晰度视频）")
    buvid3: str = Field(default="", description="B 站设备标识 Buvid3（可选，用于生成 session 参数）")
    qn: int = Field(default=0, description="清晰度设置(qn)，0 为自动（登录默认 720P，未登录默认 480P）", ge=0)
    qn_strict: bool = Field(default=False, description="是否严格按 qn 选择清晰度")
    group_at_only: bool = Field(default=False, description="群聊中仅当被 @ 时才处理 B 站链接")
    block_ai_reply: bool = Field(default=True, description="检测到 B 站视频链接后是否阻止后续 AI 回复")
    enable_video_compression: bool = Field(default=True, description="是否启用视频压缩功能")
    max_video_size_mb: int = Field(default=100, description="视频文件大小限制（MB），超过此大小将进行压缩", ge=1)
    compression_quality: int = Field(default=23, description="视频压缩质量 (1-51，数值越小质量越高)", ge=1, le=51)
    enable_duration_limit: bool = Field(default=True, description="是否启用视频时长限制")
    max_video_duration: int = Field(default=600, description="视频最大时长限制（秒），超过此时长将拒绝发送", ge=1)


class ParserConfig(PluginConfigBase):
    """解析器配置。"""
    __ui_label__ = "解析器设置"
    __ui_order__ = 2

    enable_miniapp_card: bool = Field(default=False, description="是否允许解析 B 站小卡片")


class FFmpegConfig(PluginConfigBase):
    """FFmpeg 相关配置。"""
    __ui_label__ = "FFmpeg设置"
    __ui_icon__ = "build"
    __ui_order__ = 3

    show_warnings: bool = Field(default=True, description="是否显示 FFmpeg 相关警告信息")
    enable_hardware_acceleration: bool = Field(default=True, description="是否启用硬件加速自动检测")
    force_encoder: str = Field(default="", description="强制使用特定编码器（留空则自动选择）")
    encoder_priority: list[str] = Field(
        default=["nvidia", "intel", "amd", "apple"],
        description="编码器优先级（当检测到多个硬件编码器时的选择顺序）",
    )


class EnvironmentConfig(PluginConfigBase):
    """运行环境配置。"""
    __ui_label__ = "环境设置"
    __ui_order__ = 4

    runtime_mode: str = Field(
        default="windows",
        description="运行环境模式：windows = 纯 Windows 环境 | wsl = WSL 混合环境 | linux = 纯 Linux 环境",
    )
    linux_temp_dir: str = Field(
        default="",
        description="Linux 自定义视频临时目录（留空则使用插件目录 tmp；建议填写 NapCat 可读取的目录，如 /var/tmp/maibot_bilibili）",
    )


class ApiConfig(PluginConfigBase):
    """OneBot API 配置（视频发送 fallback 用）。"""
    __ui_label__ = "API设置"
    __ui_order__ = 5

    host: str = Field(default="127.0.0.1", description="OneBot HTTP API 主机名/地址")
    port: int = Field(default=5700, description="OneBot HTTP API 端口号", ge=1, le=65535)
    token: str = Field(default="", description="OneBot HTTP API Token")


class PluginMetaConfig(PluginConfigBase):
    """插件元配置（框架必需，勿删除）。"""
    __ui_label__ = "插件设置"
    __ui_order__ = 0

    config_version: str = Field(default="2.0.3", description="配置版本（勿手动修改）")
    enabled: bool = Field(default=True, description="是否启用插件")


class PluginConfig(PluginConfigBase):
    """插件总配置。"""
    plugin: PluginMetaConfig = Field(default_factory=PluginMetaConfig)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)


# ── 插件主类 ─────────────────────────────────────────────────


class BilibiliVideoSenderPlugin(MaiBotPlugin):
    """B站视频解析与自动发送插件。"""

    config_model = PluginConfig
    config_reload_subscriptions: tuple[str, ...] = ()

    _bot_qq: str = ""
    _cached_sessdata: str = ""  # 运行时内存缓存，B站 rolling session 刷新后更新

    async def on_load(self) -> None:
        """插件加载：预热 FFmpeg 缓存。"""
        self.ctx.logger.info("Bilibili video sender plugin loading...")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ffmpeg_manager.check_ffmpeg_availability)

        # 获取 bot QQ 账号用于 @ 检测
        try:
            bot_config = await self.ctx.config.get("bot")
            if isinstance(bot_config, dict):
                self._bot_qq = str(bot_config.get("qq_account", "") or "").strip()
        except Exception:
            self._bot_qq = ""

        self.ctx.logger.info("Bilibili video sender plugin loaded")

    async def on_unload(self) -> None:
        """插件卸载：清理临时文件。"""
        self.ctx.logger.info("Bilibili video sender plugin unloading...")
        tmp_dir = get_download_temp_dir(self.config.environment.linux_temp_dir)
        try:
            for f in os.listdir(tmp_dir):
                fpath = os.path.join(tmp_dir, f)
                if os.path.isfile(fpath) and f.startswith("bilibili"):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
        except Exception:
            pass
        self.ctx.logger.info("Bilibili video sender plugin unloaded")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热更新回调。"""
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("Plugin config updated to version %s", version)

    # ── Hook 拦截 ─────────────────────────────────────────
    # chat.receive.after_process 在 message.process() 之后、maisaka 路由之前触发。
    # 此时 processed_plain_text 已填充，可用于 URL 检测。
    # 返回 {"action": "abort"} 可阻止 maisaka 处理该消息。

    @HookHandler(
        hook="chat.receive.after_process",
        name="bilibili_video_hook",
        description="在消息路由到maisaka前检测B站视频链接并处理",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_bilibili_link(self, **kwargs) -> dict[str, Any] | None:
        """在消息路由到 maisaka 前自动检测 B站链接并处理。"""
        message: dict = kwargs.get("message", {}) or {}
        config = self.config.bilibili

        # SDK MessageDict 字段：processed_plain_text 为纯文本，raw_message 为消息段列表，session_id 为会话标识
        processed_plain_text: str = message.get("processed_plain_text", "") or ""
        message_segments: list = message.get("raw_message", []) or []
        session_id: str = message.get("session_id", "") or ""

        if not session_id:
            self.ctx.logger.warning("无法获取 session_id, 跳过处理")
            return None

        # URL 检测
        url = ""
        parse_source = processed_plain_text

        if self.config.parser.enable_miniapp_card:
            for seg in message_segments:
                # SDK 2.0 消息段均为 dict；未知类型（含小卡片）序列化为 type="dict"
                if not isinstance(seg, dict):
                    continue
                if seg.get("type", "") != "dict":
                    continue
                seg_data = seg.get("data", {})
                if not isinstance(seg_data, dict):
                    continue
                source_url = str(seg_data.get("source_url", "") or "")
                if not source_url:
                    continue
                miniapp_url = BilibiliParser.find_first_bilibili_url(source_url)
                if miniapp_url:
                    parse_source = source_url
                    url = miniapp_url
                    break

        if not url:
            url = BilibiliParser.find_first_bilibili_url(processed_plain_text) or ""
            parse_source = processed_plain_text

        if not url:
            return None

        # 群聊 @ 检测
        if config.group_at_only and not self._is_private_message(message):
            if not self._is_bot_mentioned(message_segments, message):
                return None

        # 提取 qn 参数兜底
        fallback_qn = BilibiliParser.extract_qn_from_text(parse_source)
        if fallback_qn is None and parse_source != processed_plain_text:
            fallback_qn = BilibiliParser.extract_qn_from_text(processed_plain_text)

        self.ctx.logger.info("Bilibili video link detected: url=%s, qn_from_text=%s", url, fallback_qn)

        # 后台处理（不阻塞 hook 执行）
        asyncio.create_task(self._process_video_task(url, fallback_qn, session_id, message))

        # 根据配置决定是否阻止 maisaka
        if config.block_ai_reply:
            return {"action": "abort"}
        return None

    # ── 视频处理流水线 ────────────────────────────────────

    async def _process_video_task(
        self,
        url: str,
        fallback_qn: int | None,
        session_id: str,
        message: dict[str, Any],
    ) -> None:
        """完整的视频处理流水线（在后台 task 中运行）。"""
        config = self.config.bilibili
        try:
            loop = asyncio.get_running_loop()

            # Step 1: 解析视频信息 + 获取播放地址（阻塞）
            info, sources, selected_qn_name, status, error_msg = await loop.run_in_executor(
                None, self._resolve_video_sync, url, fallback_qn
            )

            if status == "unsupported_type":
                self.ctx.logger.info("Ignoring unsupported Bilibili link type")
                return

            if not info or not sources:
                await send_text(self.ctx, error_msg or "未能解析该视频链接，请稍后重试。", session_id, message, self.config.api)
                return

            # Step 2: 时长校验
            if config.enable_duration_limit and info.duration is not None:
                if info.duration > config.max_video_duration:
                    duration_min = int(info.duration // 60)
                    duration_sec = int(info.duration % 60)
                    max_min = int(config.max_video_duration // 60)
                    max_sec = int(config.max_video_duration % 60)
                    await send_text(
                        self.ctx,
                        f"视频时长超过限制：视频时长为 {duration_min}分{duration_sec}秒，"
                        f"最大允许时长为 {max_min}分{max_sec}秒。",
                        session_id,
                        message,
                        self.config.api,
                    )
                    return

            # Step 3: 通知用户解析成功
            success_msg = "解析成功"
            if selected_qn_name:
                success_msg = f"解析成功，已选择：{selected_qn_name}"
            await send_text(self.ctx, success_msg, session_id, message, self.config.api)

            # Step 4: 下载 + 合并（阻塞）
            sessdata = str(config.sessdata).strip()
            buvid3 = str(config.buvid3).strip()
            temp_path = await loop.run_in_executor(
                None, download_video, info, sources, sessdata, buvid3, self.config.environment.linux_temp_dir
            )
            if not temp_path:
                await send_text(self.ctx, "视频下载失败，请稍后重试。", session_id, message, self.config.api)
                return

            self.ctx.logger.info("Video download completed: %s", temp_path)

            # Step 5: 时长二次校验（使用 ffprobe）
            video_duration = await loop.run_in_executor(None, BilibiliParser.get_video_duration, temp_path)
            if config.enable_duration_limit and video_duration is not None:
                if video_duration > config.max_video_duration:
                    duration_min = int(video_duration // 60)
                    duration_sec = int(video_duration % 60)
                    max_min = int(config.max_video_duration // 60)
                    max_sec = int(config.max_video_duration % 60)
                    await send_text(
                        self.ctx,
                        f"视频时长超过限制：视频时长为 {duration_min}分{duration_sec}秒，"
                        f"最大允许时长为 {max_min}分{max_sec}秒，已拒绝发送。",
                        session_id,
                        message,
                        self.config.api,
                    )
                    await loop.run_in_executor(None, lambda: os.remove(temp_path) if os.path.exists(temp_path) else None)
                    return

            # Step 6: 文件大小检查 + 压缩（阻塞）
            final_path = await loop.run_in_executor(
                None, self._maybe_compress, temp_path
            )
            await loop.run_in_executor(None, ensure_shared_file_permissions, final_path)

            # Step 7: 发送
            sent_ok = await send_video(
                self.ctx,
                final_path,
                self.config.environment.runtime_mode,
                message,
                self.config.api,
            )
            if not sent_ok:
                await send_text(self.ctx, "视频解析成功，但发送失败。请检查网络连接和API配置。", session_id, message, self.config.api)
            else:
                self.ctx.logger.info("Video file sent successfully")

            # Step 8: 清理
            await loop.run_in_executor(None, self._cleanup_files, final_path, temp_path)

            self.ctx.logger.info("Bilibili video processing completed")

        except Exception:
            self.ctx.logger.error("视频处理异常", exc_info=True)
            try:
                await send_text(self.ctx, "视频处理过程中发生错误，请稍后重试。", session_id, message, self.config.api)
            except Exception:
                pass

    # ── 同步辅助方法（在线程池中运行） ──────────────────────

    def _resolve_video_sync(
        self,
        url: str,
        fallback_qn: int | None,
    ) -> tuple[BilibiliVideoInfo | None, dict[str, Any] | None, str | None, str, str | None]:
        """同步解析视频链接（阻塞，在线程池中运行）。"""
        # 预热 FFmpeg 缓存（check_ffmpeg_availability 幂等，结果已内部缓存）
        ffmpeg_manager.check_ffmpeg_availability()

        # 优先使用内存缓存的 SESSDATA（B站 rolling session 刷新后更新）
        effective_sessdata = self._cached_sessdata or str(self.config.bilibili.sessdata).strip()
        config_opts = {
            "qn": BilibiliParser.safe_int(self.config.bilibili.qn),
            "qn_strict": self.config.bilibili.qn_strict,
            "sessdata": effective_sessdata,
            "buvid3": str(self.config.bilibili.buvid3).strip(),
        }

        # 执行配置验证，输出警告与建议日志
        validation_result = BilibiliParser.validate_config(config_opts)
        if not validation_result["valid"]:
            self.ctx.logger.error("配置验证失败，但继续尝试处理")
        for w in validation_result["warnings"]:
            self.ctx.logger.debug("配置警告: %s", w)
        for r in validation_result["recommendations"]:
            self.ctx.logger.debug("配置建议: %s", r)

        target_url = url

        # 短链接解析（带重试）
        if "b23.tv" in target_url:
            for attempt in range(3):
                try:
                    target_url = BilibiliParser._follow_redirect(target_url)
                    break
                except Exception as e:
                    if attempt < 2:
                        time_sleep = __import__("time").sleep
                        time_sleep(attempt + 1)
                    else:
                        # 使用原始 URL 继续
                        pass

        target_url = BilibiliParser._sanitize_url(target_url)

        # 检查是否为支持的视频链接
        has_bv = BilibiliParser._extract_bvid(target_url) is not None
        has_av = re.search(r"/video/av\d+", target_url) is not None
        if not (has_bv or has_av):
            return None, None, None, "unsupported_type", None

        # URL 参数覆盖 qn
        url_qn = BilibiliParser.extract_qn_param(target_url)
        if url_qn is None and fallback_qn is not None:
            url_qn = fallback_qn
        if url_qn is not None:
            config_opts["qn"] = url_qn

        # 视频信息解析（带重试）
        info = None
        for attempt in range(3):
            try:
                info = BilibiliParser.get_view_info_by_url(target_url, config_opts)
                if info:
                    break
            except Exception:
                pass
            if attempt < 2:
                __import__("time").sleep(attempt + 1)

        if not info:
            return None, None, None, "error", f"未能解析该视频链接，请稍后重试。"

        sources, status = BilibiliParser.get_play_urls(info.aid, info.cid, config_opts)
        if not sources:
            return info, None, None, "error", f"解析失败：{status}"

        # 若 B站在响应中刷新了 SESSDATA，更新内存缓存并持久化到 config.toml
        if config_opts.get("sessdata_refreshed"):
            new_sessdata = config_opts.get("sessdata", "")
            if new_sessdata:
                self._cached_sessdata = new_sessdata
                self.ctx.logger.info("SESSDATA 内存缓存已更新（B站 rolling session）")
                self._save_sessdata_to_config(new_sessdata)

        selected_qn_name = config_opts.get("selected_qn_name")
        return info, sources, selected_qn_name, status, None

    def _maybe_compress(self, temp_path: str) -> str:
        """按需压缩视频（同步，在线程池中运行）。"""
        config = self.config.bilibili
        ffmpeg_cfg = self.config.ffmpeg

        try:
            video_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
        except Exception:
            return temp_path

        if video_size_mb <= config.max_video_size_mb or not config.enable_video_compression:
            return temp_path

        ffmpeg_info = ffmpeg_manager.check_ffmpeg_availability()
        if not ffmpeg_info["ffmpeg_available"]:
            return temp_path

        base_name, _ = os.path.splitext(temp_path)
        compressed_path = f"{base_name}_compressed.mp4"

        compressor = VideoCompressor(
            ffmpeg_path=ffmpeg_info["ffmpeg_path"],
            enable_hardware=ffmpeg_cfg.enable_hardware_acceleration,
            force_encoder=ffmpeg_cfg.force_encoder,
            encoder_priority=ffmpeg_cfg.encoder_priority,
        )

        if compressor.compress_video(temp_path, compressed_path, config.max_video_size_mb, config.compression_quality):
            compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
            self.ctx.logger.info(
                "Video compression: %.2fMB -> %.2fMB", video_size_mb, compressed_size_mb
            )
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return compressed_path

        return temp_path

    def _save_sessdata_to_config(self, new_sessdata: str) -> None:
        """将 B站刷新的 SESSDATA 写回 config.toml，供插件重启后继续使用。"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.toml")
            if not os.path.exists(config_path):
                self.ctx.logger.warning("config.toml 不存在，SESSDATA 仅保留在内存缓存")
                return
            with open(config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            in_bilibili = False
            updated = False
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("["):
                    in_bilibili = (stripped == "[bilibili]")
                if in_bilibili and not updated and re.match(r"\s*sessdata\s*=", line):
                    line = re.sub(r'(".*?"|\'.*?\')', f'"{new_sessdata}"', line, count=1)
                    updated = True
                new_lines.append(line)
            if updated:
                with open(config_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                self.ctx.logger.info("SESSDATA 已持久化到 config.toml")
            else:
                self.ctx.logger.warning("config.toml 中未找到 [bilibili].sessdata，无法持久化")
        except Exception as e:
            self.ctx.logger.warning("持久化 SESSDATA 失败: %s", e)

    @staticmethod
    def _cleanup_files(final_path: str, temp_path: str) -> None:
        """清理临时文件（同步，在线程池中运行）。"""
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            if final_path != temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

    # ── 消息工具方法 ──────────────────────────────────────

    @staticmethod
    def _is_private_message(message: dict) -> bool:
        """检测消息是否为私聊。

        SDK MessageDict 中私聊消息的 message_info.group_info 为 None，群聊则有值。
        """
        message_info = message.get("message_info", {})
        if not message_info:
            return False
        return message_info.get("group_info") is None

    def _is_bot_mentioned(self, segments: list, message: dict) -> bool:
        """判断消息是否 @ 了机器人。"""
        # 从顶层 is_at / is_mentioned 字段快速判定
        if message.get("is_at", False) or message.get("is_mentioned", False):
            return True

        # 从结构化消息段兜底（SDK 2.0 消息段均为 dict，@ 类型为 "at"）
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type", "") != "at":
                continue
            seg_data = seg.get("data", {})
            if not isinstance(seg_data, dict):
                continue
            target_user_id = str(seg_data.get("target_user_id", "") or "").strip()
            if self._bot_qq and target_user_id == self._bot_qq:
                return True

        return False


# ── SDK 入口工厂函数 ──────────────────────────────────────


def create_plugin() -> BilibiliVideoSenderPlugin:
    """SDK 入口工厂函数。"""
    return BilibiliVideoSenderPlugin()
