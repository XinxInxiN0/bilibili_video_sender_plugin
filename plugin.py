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
from .utils import get_download_temp_dir


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
    store_plugin_text: bool = Field(default=False, description="插件发送的文本消息是否写入历史记录")
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

    config_version: str = Field(default="2.0.0", description="配置版本（勿手动修改）")
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
        tmp_dir = get_download_temp_dir()
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
            temp_path = await loop.run_in_executor(None, download_video, info, sources, sessdata, buvid3)
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

        config_opts = {
            "qn": BilibiliParser.safe_int(self.config.bilibili.qn),
            "qn_strict": self.config.bilibili.qn_strict,
            "sessdata": str(self.config.bilibili.sessdata).strip(),
            "buvid3": str(self.config.bilibili.buvid3).strip(),
        }

        target_url = url

        # 短链接解析（带重试）
        if "b23.tv" in target_url:
            for attempt in range(3):
                try:
<<<<<<< HEAD
                    target_url = BilibiliParser._follow_redirect(target_url)
                    break
=======
                    result = await loop.run_in_executor(None, _blocking)
                except Exception as exc:  # noqa: BLE001 - 简要兜底
                    error_msg = f"解析失败：{exc}"
                    self._logger.error(error_msg)
                    await self._send_text(error_msg, stream_id)
                    return self._make_return_value(True, continue_processing, "解析失败")

                if not result:
                    error_msg = "未能解析该视频链接，请稍后重试。"
                    self._logger.error(error_msg)
                    await self._send_text(error_msg, stream_id)
                    return self._make_return_value(True, continue_processing, "解析失败")

                info, sources, status = result
                if status == "unsupported_type":
                    self._logger.info("Ignoring unsupported Bilibili link type (e.g. Live room)")
                    return self._make_return_value(True, continue_processing, "跳过非视频链接")

                if not sources:
                    error_msg = f"解析失败：{status}"
                    self._logger.error(error_msg)
                    await self._send_text(error_msg, stream_id)
                    return self._make_return_value(True, continue_processing, "解析失败")

                self._logger.info(f"Parse successful: {info.title}")

                # 早期时长校验 (使用 API 返回的时长)
                enable_duration_limit = self.get_config("bilibili.enable_duration_limit", True)
                max_video_duration = self.get_config("bilibili.max_video_duration", 600)

                if enable_duration_limit and info.duration is not None:
                    if info.duration > max_video_duration:
                        duration_minutes = int(info.duration // 60)
                        duration_seconds = int(info.duration % 60)
                        max_minutes = int(max_video_duration // 60)
                        max_seconds = int(max_video_duration % 60)

                        error_msg = (
                            f"视频时长超过限制：视频时长为 {duration_minutes}分{duration_seconds}秒，"
                            f"最大允许时长为 {max_minutes}分{max_seconds}秒。"
                        )
                        self._logger.warning(
                            f"Video duration (API) exceeds limit: {info.duration}s > {max_video_duration}s"
                        )
                        await self._send_text(error_msg, stream_id)
                        return self._make_return_value(True, continue_processing, "视频时长超过限制(API)")
                    else:
                        self._logger.debug(
                            f"Early duration check passed (API): {info.duration}s <= {max_video_duration}s"
                        )

                # 发送解析成功消息
                success_message = "解析成功"
                selected_qn_name = config_opts.get("selected_qn_name")
                selected_qn = config_opts.get("selected_qn")
                if selected_qn_name and selected_qn:
                    success_message = f"解析成功，已选择：{selected_qn_name}"
                await self._send_text(success_message, stream_id)

                # 现在获取FFmpeg信息（应该是瞬间完成，因为_blocking已经在后台初始化了缓存）
                ffmpeg_info = _ffmpeg_manager.check_ffmpeg_availability()

                # 显示警告（如果需要）
                show_ffmpeg_warnings = self.get_config("ffmpeg.show_warnings", True)
                if show_ffmpeg_warnings:
                    if not ffmpeg_info["ffmpeg_available"]:
                        self._logger.debug("FFmpeg unavailable, merge functions disabled")
                    if not ffmpeg_info["ffprobe_available"]:
                        self._logger.debug("ffprobe unavailable, duration detection disabled")

                # 同时发送视频文件
                self._logger.debug("Starting video download...")

                def _download_to_temp(sources: Dict[str, Any]) -> Optional[str]:
                    try:
                        # sources 结构：dash -> {video_urls, audio_urls}；durl -> {urls}
                        # 生成安全的临时文件名，避免特殊字符导致写入失败
                        safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", info.title).strip() or "bilibili_video"
                        unique_tag = f"{info.aid}_{info.cid}_{int(time.time() * 1000)}"
                        base_name = f"{safe_title}_{unique_tag}"
                        tmp_dir = _get_download_temp_dir()
                        os.makedirs(tmp_dir, exist_ok=True)

                        temp_path = os.path.join(tmp_dir, f"{base_name}.mp4")

                        self._logger.debug("Preparing download", title=info.title, temp_path=temp_path)

                        # 构建下载请求头（必要时带 Cookie）
                        headers = {
                            "User-Agent": BilibiliParser.USER_AGENT,
                            "Referer": "https://www.bilibili.com/",
                            "Origin": "https://www.bilibili.com",
                            "Accept": "*/*",
                            "Accept-Encoding": "gzip, deflate, br",
                            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                            "Range": "bytes=0-",  # 支持 Range 请求，避免部分 CDN 403
                        }
                        sessdata_hdr = self.get_config("bilibili.sessdata", "").strip()
                        buvid3_hdr = self.get_config("bilibili.buvid3", "").strip()
                        if sessdata_hdr:
                            cookie_parts = [f"SESSDATA={sessdata_hdr}"]
                            if buvid3_hdr:
                                cookie_parts.append(f"buvid3={buvid3_hdr}")
                            headers["Cookie"] = "; ".join(cookie_parts)
                            self._logger.debug("Cookie auth added for download")
                        else:
                            self._logger.debug("No Cookie for download, may get 403 error")

                        # 下载单条流，支持多 URL 自动切换（主链失败时自动切备链）
                        def _download_stream(url_list: List[str], save_path: str, desc: str) -> bool:
                            if not url_list:
                                return False
                            last_err = None
                            for idx, url in enumerate(url_list, start=1):
                                try:
                                    req = BilibiliParser._build_request(url, headers)
                                    with urllib.request.urlopen(req, timeout=60) as resp:
                                        # 获取文件总大小（如果可用）
                                        total_size = resp.headers.get("content-length")
                                        total_size = int(total_size) if total_size else 0

                                        # 创建进度条
                                        progress_bar = ProgressBar(
                                            total_size, f"{desc} (候选{idx}/{len(url_list)})", 30
                                        )
                                        with open(save_path, "wb") as f:
                                            downloaded = 0
                                            while True:
                                                chunk = resp.read(1024 * 256)
                                                if not chunk:
                                                    break
                                                f.write(chunk)
                                                downloaded += len(chunk)
                                                # 使用进度条显示进度
                                                progress_bar.update(downloaded)

                                        # 完成进度条显示
                                        progress_bar.finish()
                                        if total_size > 0 and downloaded < total_size:
                                            raise IOError(f"下载不完整: {downloaded}/{total_size}")
                                    return True
                                except Exception as e:
                                    last_err = e
                                    self._logger.warning(f"{desc} 第{idx}条链接下载失败: {e}")
                                    try:
                                        if os.path.exists(save_path):
                                            os.remove(save_path)
                                    except Exception:
                                        pass
                            if last_err:
                                self._logger.error(f"{desc} 所有链接下载失败: {last_err}")
                            return False

                        source_type = sources.get("type")

                        # DASH：先下载视频流，再尝试下载音频流
                        if source_type == "dash":
                            video_urls = sources.get("video_urls") or []
                            audio_urls = sources.get("audio_urls") or []

                            self._logger.debug(
                                "DASH format assumed", video_urls=len(video_urls), audio_urls=len(audio_urls)
                            )

                            video_temp = os.path.join(tmp_dir, f"{base_name}_video.m4s")
                            if not _download_stream(video_urls, video_temp, "Video stream downloading"):
                                return None

                            audio_temp = None
                            if audio_urls:
                                audio_temp = os.path.join(tmp_dir, f"{base_name}_audio.m4s")
                                if not _download_stream(audio_urls, audio_temp, "Audio stream downloading"):
                                    self._logger.warning("Audio stream download failed, continue with video only")
                                    audio_temp = None

                            # 尝试使用 FFmpeg 合并音视频
                            try:
                                ffmpeg_path = _ffmpeg_manager.get_ffmpeg_path()
                                if ffmpeg_path:
                                    self._logger.debug(f"Using FFmpeg: {ffmpeg_path}")
                                    ffprobe_path = _ffmpeg_manager.get_ffprobe_path()
                                    video_format = "unknown"
                                    if ffprobe_path:
                                        probe_cmd = [
                                            ffprobe_path,
                                            "-v",
                                            "error",
                                            "-show_entries",
                                            "format=format_name",
                                            "-of",
                                            "default=noprint_wrappers=1:nokey=1",
                                            video_temp,
                                        ]
                                        try:
                                            video_format = (
                                                subprocess.run(probe_cmd, capture_output=True, text=False)
                                                .stdout.decode("utf-8", errors="replace")
                                                .strip()
                                            )
                                        except Exception as e:
                                            self._logger.warning(f"Unable to check video format: {str(e)}")
                                        if audio_temp and os.path.exists(audio_temp):
                                            probe_cmd = [
                                                ffprobe_path,
                                                "-v",
                                                "error",
                                                "-show_entries",
                                                "format=format_name",
                                                "-of",
                                                "default=noprint_wrappers=1:nokey=1",
                                                audio_temp,
                                            ]
                                            try:
                                                (
                                                    subprocess.run(probe_cmd, capture_output=True, text=False)
                                                    .stdout.decode("utf-8", errors="replace")
                                                    .strip()
                                                )
                                            except Exception as e:
                                                self._logger.warning(f"Unable to check audio format: {str(e)}")
                                    else:
                                        self._logger.warning(
                                            f"ffprobe not found, unable to check file format: {ffprobe_path}"
                                        )

                                    if "m4s" in video_format.lower() or video_temp.lower().endswith(".m4s"):
                                        if audio_temp and os.path.exists(audio_temp):
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i",
                                                video_temp,
                                                "-i",
                                                audio_temp,
                                                "-c:v",
                                                "copy",  # 复制视频流，不重新编码
                                                "-c:a",
                                                "aac",  # 将音频转换为aac格式以确保兼容性
                                                "-strict",
                                                "experimental",
                                                "-b:a",
                                                "192k",  # 设置音频比特率
                                                "-y",
                                                temp_path,
                                            ]
                                        else:
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i",
                                                video_temp,
                                                "-c:v",
                                                "copy",
                                                "-y",
                                                temp_path,
                                            ]
                                    else:
                                        if audio_temp and os.path.exists(audio_temp):
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i",
                                                video_temp,
                                                "-i",
                                                audio_temp,
                                                "-c:v",
                                                "copy",
                                                "-c:a",
                                                "copy",
                                                "-y",
                                                temp_path,
                                            ]
                                        else:
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i",
                                                video_temp,
                                                "-c:v",
                                                "copy",
                                                "-y",
                                                temp_path,
                                            ]

                                    self._logger.debug("Starting to merge video and audio...")
                                    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=False)
                                    if result.returncode == 0:
                                        self._logger.debug("Video and audio merged successfully")
                                        try:
                                            if os.path.exists(video_temp):
                                                os.remove(video_temp)
                                            if audio_temp and os.path.exists(audio_temp):
                                                os.remove(audio_temp)
                                            self._logger.debug("Temporary files cleaned")
                                        except Exception as e:
                                            self._logger.warning(f"Failed to clean temp: {str(e)}")
                                        return temp_path
                                    else:
                                        stderr_text = (
                                            result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                                        )
                                        self._logger.warning(f"FFmpeg merge failed: {stderr_text}")
                                        # 合并失败时退化为仅视频转封装，避免发送 m4s
                                        fallback_cmd = [ffmpeg_path, "-i", video_temp, "-c", "copy", "-y", temp_path]
                                        fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=False)
                                        if fallback_result.returncode == 0:
                                            self._logger.warning("Audio merge failed, fallback to video-only mp4")
                                            try:
                                                if os.path.exists(video_temp):
                                                    os.remove(video_temp)
                                                if audio_temp and os.path.exists(audio_temp):
                                                    os.remove(audio_temp)
                                            except Exception:
                                                pass
                                            return temp_path
                                        fallback_err = (
                                            fallback_result.stderr.decode("utf-8", errors="replace")
                                            if fallback_result.stderr
                                            else ""
                                        )
                                        self._logger.warning(f"FFmpeg fallback remux failed: {fallback_err}")
                                else:
                                    self._logger.warning("FFmpeg not found, cannot merge video and audio")
                            except Exception as e:
                                self._logger.warning(f"Merge failed: {str(e)}")

                            self._logger.warning("DASH 合并失败且无法转封装，放弃发送 m4s")
                            if audio_temp and os.path.exists(audio_temp):
                                try:
                                    os.remove(audio_temp)
                                except Exception:
                                    pass
                            if os.path.exists(video_temp):
                                try:
                                    os.remove(video_temp)
                                except Exception:
                                    pass
                            return None

                        # durl：单文件直链下载（可能带备链）
                        if source_type == "durl":
                            url_list = sources.get("urls") or []
                            if not url_list:
                                return None
                            parsed_path = urllib.parse.urlparse(url_list[0]).path
                            ext = os.path.splitext(parsed_path)[1].lower()
                            download_path = temp_path
                            if ext and ext != ".mp4":
                                download_path = os.path.join(tmp_dir, f"{base_name}{ext}")

                            self._logger.debug("Single file download", path=download_path)
                            if not _download_stream(url_list, download_path, "Video downloading"):
                                return None

                            final_path = download_path
                            if ext and ext != ".mp4":
                                ffmpeg_path = _ffmpeg_manager.get_ffmpeg_path()
                                if ffmpeg_path:
                                    remux_path = os.path.join(tmp_dir, f"{base_name}.mp4")
                                    remux_cmd = [ffmpeg_path, "-i", download_path, "-c", "copy", "-y", remux_path]
                                    remux_result = subprocess.run(remux_cmd, capture_output=True, text=False)
                                    if remux_result.returncode == 0:
                                        self._logger.debug("Single file remuxed to mp4")
                                        try:
                                            os.remove(download_path)
                                        except Exception:
                                            pass
                                        final_path = remux_path
                                    else:
                                        stderr_text = (
                                            remux_result.stderr.decode("utf-8", errors="replace")
                                            if remux_result.stderr
                                            else ""
                                        )
                                        self._logger.warning(f"Single file remux failed: {stderr_text}")
                                else:
                                    self._logger.debug("FFmpeg not found, skipping remux")

                            return final_path

                        self._logger.debug("No supported streams found for download")
                        return None
                    except Exception as e:
                        self._logger.error(f"Failed to download video: {e}")
                        return None

                temp_path = await asyncio.get_running_loop().run_in_executor(None, lambda: _download_to_temp(sources))
                if not temp_path:
                    self._logger.warning("Video download failed")
                    return self._make_return_value(True, continue_processing, "视频下载失败")

                self._logger.debug(f"Video download completed: {temp_path}")

                # 检查视频时长 (异步执行，防止阻塞主循环)
                video_duration = await loop.run_in_executor(None, BilibiliParser.get_video_duration, temp_path)
                self._logger.debug(f"Detected video duration: {video_duration} seconds")

                # 检查视频时长限制
                enable_duration_limit = self.get_config("bilibili.enable_duration_limit", True)
                max_video_duration = self.get_config("bilibili.max_video_duration", 600)

                if enable_duration_limit and video_duration is not None:
                    if video_duration > max_video_duration:
                        duration_minutes = int(video_duration // 60)
                        duration_seconds = int(video_duration % 60)
                        max_minutes = int(max_video_duration // 60)
                        max_seconds = int(max_video_duration % 60)

                        error_msg = (
                            f"视频时长超过限制：视频时长为 {duration_minutes}分{duration_seconds}秒，"
                            f"最大允许时长为 {max_minutes}分{max_seconds}秒，已拒绝发送。"
                        )
                        self._logger.warning(f"Video duration exceeds limit: {video_duration}s > {max_video_duration}s")
                        await self._send_text(error_msg, stream_id)

                        # 清理临时文件 (异步)
                        try:
                            await loop.run_in_executor(
                                None, lambda: os.remove(temp_path) if os.path.exists(temp_path) else None
                            )
                            self._logger.debug("Temporary video file deleted after duration check failure")
                        except Exception as e:
                            self._logger.warning(f"Failed to delete temporary file: {e}")

                        return self._make_return_value(True, continue_processing, "视频时长超过限制")
                    else:
                        self._logger.debug(f"Video duration check passed: {video_duration}s <= {max_video_duration}s")
                elif enable_duration_limit and video_duration is None:
                    self._logger.warning("Duration limit enabled but ffprobe unavailable, skipping duration check")

                # 检查视频文件大小和时长，决定处理策略 (异步获取大小)
                try:
                    video_size_mb = await loop.run_in_executor(None, lambda: os.path.getsize(temp_path) / (1024 * 1024))
                except Exception:
                    video_size_mb = 0.0

                self._logger.debug(f"Detected video size: {video_size_mb:.2f}MB")

                # 从配置读取相关设置
                max_video_size_mb = self.get_config("bilibili.max_video_size_mb", 100)
                enable_compression = self.get_config("bilibili.enable_video_compression", True)
                compression_quality = self.get_config("bilibili.compression_quality", 23)

                self._logger.debug(
                    f"Video processing configuration: compression={enable_compression}, "
                    f"max size={max_video_size_mb}MB, compression quality={compression_quality}"
                )

                # 处理单个视频文件
                final_video_path = temp_path

                # 定义压缩任务函数（在线程池中运行，避免阻塞主进程）
                def _compress_task() -> Optional[str]:
                    try:
                        self._logger.debug(
                            f"Single video file size ({video_size_mb:.2f}MB) exceeds limit, starting compression..."
                        )

                        base_name, _ = os.path.splitext(temp_path)
                        compressed_path = f"{base_name}_compressed.mp4"
                        # 构建配置字典传递给压缩器
                        config_dict = {
                            "ffmpeg": {
                                "enable_hardware_acceleration": self.get_config(
                                    "ffmpeg.enable_hardware_acceleration", True
                                ),
                                "force_encoder": self.get_config("ffmpeg.force_encoder", ""),
                                "encoder_priority": self.get_config(
                                    "ffmpeg.encoder_priority", ["nvidia", "intel", "amd", "apple"]
                                ),
                            }
                        }
                        compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"], config_dict)

                        # 执行压缩（这是最耗时的部分）
                        if compressor.compress_video(
                            temp_path, compressed_path, max_video_size_mb, compression_quality
                        ):
                            compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                            self._logger.debug(
                                f"Single video compression successful: {video_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB"
                            )

                            # 压缩成功后清理原始文件
                            try:
                                os.remove(temp_path)
                                self._logger.debug(f"Original video file {temp_path} deleted")
                            except Exception as e:
                                self._logger.warning(f"Failed to delete original video file: {e}")

                            return compressed_path
                        else:
                            self._logger.debug("Single video compression failed, using original file")
                            return None
                    except Exception as e:
                        self._logger.error(f"Compression task error: {e}")
                        return None

                # 如果文件过大且启用压缩，先压缩
                if video_size_mb > max_video_size_mb and enable_compression and ffmpeg_info["ffmpeg_available"]:
                    # 将繁重的压缩任务扔进线程池
                    compressed_result = await loop.run_in_executor(None, _compress_task)

                    if compressed_result:
                        final_video_path = compressed_result

                elif video_size_mb > max_video_size_mb:
                    self._logger.debug(
                        f"Single video file size ({video_size_mb:.2f}MB) exceeds limit but compression not available"
                    )
                else:
                    self._logger.debug(
                        f"Single video file size ({video_size_mb:.2f}MB) meets requirements, no compression needed"
                    )

                # 发送处理后的视频文件
                async def _try_send(path: str) -> bool:
                    # 根据运行环境模式进行路径转换
                    runtime_mode = self.get_config("environment.runtime_mode", "windows")
                    converted_path = convert_windows_to_wsl_path(path) if runtime_mode == "wsl" else path

                    self._logger.debug(f"Sending single video - runtime mode: {runtime_mode}")
                    self._logger.debug(f"Sending single video - original path: {path}")
                    self._logger.debug(f"Sending single video - converted path: {converted_path}")

                    # 检查是否为私聊消息
                    is_private = self._is_private_message(message)

                    if is_private:
                        # 私聊消息，使用专用API发送
                        user_id = self._get_user_id(message)
                        if user_id:
                            self._logger.debug(
                                f"Private message detected, sending private video API to user: {user_id}"
                            )
                            return await self._send_private_video(path, converted_path, user_id)
                        else:
                            self._logger.error("Private message but unable to get user ID")
                            return False
                    else:
                        # 群聊消息，使用群视频API
                        group_id = self._get_group_id(message)
                        if group_id:
                            self._logger.debug(f"Group message detected, sending group video API to group: {group_id}")
                            return await self._send_group_video(path, converted_path, group_id)
                        else:
                            self._logger.error("Group message detected but unable to get group ID, sending failed")
                            return False

                sent_ok = await _try_send(final_video_path)
                if not sent_ok:
                    self._logger.debug("Video sending failed")
                    await self._send_text("视频解析成功，但发送失败。请检查网络连接和API配置。", stream_id)
                else:
                    self._logger.info("Video file sent successfully")

                # 删除临时文件 (异步)
                try:

                    def _cleanup():
                        # 删除最终处理的文件
                        if os.path.exists(final_video_path):
                            os.remove(final_video_path)
                        # 如果还有原始文件且不同于最终文件，也删除
                        if final_video_path != temp_path and os.path.exists(temp_path):
                            os.remove(temp_path)

                    await loop.run_in_executor(None, _cleanup)
                    self._logger.debug("Video files cleaned up")
>>>>>>> 179a4a319dc6af81fac7f55a25f53670690da6af
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
