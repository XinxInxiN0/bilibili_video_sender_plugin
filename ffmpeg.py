# -*- coding: utf-8 -*-
"""跨平台 FFmpeg 管理与视频压缩。"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("plugin.bilibili_video_sender.ffmpeg")


class FFmpegManager:
    """跨平台 FFmpeg 管理器。"""

    def __init__(self):
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.system = platform.system().lower()
        self.ffmpeg_dir = os.path.join(self.plugin_dir, "ffmpeg")

    def get_ffmpeg_path(self) -> Optional[str]:
        """获取 ffmpeg 可执行文件路径。"""
        return self._get_executable_path("ffmpeg")

    def get_ffprobe_path(self) -> Optional[str]:
        """获取 ffprobe 可执行文件路径。"""
        return self._get_executable_path("ffprobe")

    def _get_executable_path(self, executable_name: str) -> Optional[str]:
        """根据操作系统获取可执行文件路径。"""
        if self.system == "windows":
            bin_dir = os.path.join(self.ffmpeg_dir, "bin")
            executable_path = os.path.join(bin_dir, f"{executable_name}.exe")
        elif self.system in ("linux", "darwin"):
            platform_bin_dir = os.path.join(self.ffmpeg_dir, "bin", self.system)
            executable_path = os.path.join(platform_bin_dir, executable_name)
            if not os.path.exists(executable_path):
                bin_dir = os.path.join(self.ffmpeg_dir, "bin")
                executable_path = os.path.join(bin_dir, executable_name)
        else:
            _logger.warning("不支持的操作系统: %s", self.system)
            return None

        if os.path.exists(executable_path):
            _logger.debug("Found bundled %s: %s", executable_name, executable_path)
            return executable_path

        system_executable = shutil.which(executable_name)
        if system_executable:
            _logger.debug("Found system %s: %s", executable_name, system_executable)
            return system_executable

        _logger.warning("未找到 %s 可执行文件", executable_name)
        return None

    _cached_check_result: Optional[Dict[str, Any]] = None

    def check_hardware_encoders(self) -> Dict[str, Any]:
        """检测可用的硬件编码器（带缓存）。"""
        if self._cached_check_result is not None:
            return self._cached_check_result

        ffmpeg_path = self.get_ffmpeg_path()
        if not ffmpeg_path:
            return {"available_encoders": [], "recommended_encoder": "libx264"}

        available_encoders: List[Dict[str, Any]] = []

        encoders_to_check = [
            {"name": "h264_nvenc", "type": "nvidia", "codec": "h264", "description": "NVIDIA H.264硬件编码"},
            {"name": "hevc_nvenc", "type": "nvidia", "codec": "h265", "description": "NVIDIA H.265硬件编码"},
            {"name": "h264_qsv", "type": "intel", "codec": "h264", "description": "Intel QSV H.264硬件编码"},
            {"name": "hevc_qsv", "type": "intel", "codec": "h265", "description": "Intel QSV H.265硬件编码"},
            {"name": "h264_amf", "type": "amd", "codec": "h264", "description": "AMD H.264硬件编码"},
            {"name": "hevc_amf", "type": "amd", "codec": "h265", "description": "AMD H.265硬件编码"},
            {"name": "h264_videotoolbox", "type": "apple", "codec": "h264", "description": "Apple H.264硬件编码"},
            {"name": "hevc_videotoolbox", "type": "apple", "codec": "h265", "description": "Apple H.265硬件编码"},
        ]

        try:
            cmd = [ffmpeg_path, "-encoders"]
            process = subprocess.run(cmd, capture_output=True, text=False, timeout=15)

            if process.returncode == 0:
                encoders_output = process.stdout.decode("utf-8", errors="replace")
                for encoder in encoders_to_check:
                    if encoder["name"] in encoders_output:
                        if self._test_encoder(ffmpeg_path, encoder["name"]):
                            available_encoders.append(encoder)
                            _logger.debug("Found available encoder: %s", encoder["description"])
                        else:
                            _logger.debug("Encoder %s exists but unavailable", encoder["name"])
            else:
                stderr_text = process.stderr.decode("utf-8", errors="replace") if process.stderr else ""
                _logger.warning("获取编码器列表失败: %s", stderr_text)

        except Exception as e:
            _logger.warning("检测硬件编码器时发生错误: %s", e)

        recommended_encoder = self._get_recommended_encoder(available_encoders)

        result = {
            "available_encoders": available_encoders,
            "recommended_encoder": recommended_encoder,
            "total_hardware_encoders": len(available_encoders),
        }

        self._cached_check_result = result
        _logger.debug(
            "Hardware encoder detection complete: %d available, recommend: %s",
            len(available_encoders),
            recommended_encoder,
        )
        return result

    def _test_encoder(self, ffmpeg_path: str, encoder_name: str) -> bool:
        """测试编码器是否真正可用。"""
        try:
            cmd = [
                ffmpeg_path,
                "-f", "lavfi",
                "-i", "testsrc=duration=1:size=320x240:rate=1",
                "-c:v", encoder_name,
                "-t", "1",
                "-f", "null", "-",
            ]
            process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
            return process.returncode == 0
        except Exception:
            return False

    def _get_recommended_encoder(self, available_encoders: List[Dict[str, Any]]) -> str:
        """根据可用编码器选择推荐的编码器。"""
        if not available_encoders:
            return "libx264"

        priority_order = ["nvidia", "intel", "amd", "apple"]
        for encoder_type in priority_order:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]

        return available_encoders[0]["name"]

    _cached_availability_result: Optional[Dict[str, Any]] = None

    def check_ffmpeg_availability(self) -> Dict[str, Any]:
        """检查 FFmpeg 可用性（带缓存）。"""
        if self._cached_availability_result is not None:
            return self._cached_availability_result

        result: Dict[str, Any] = {
            "ffmpeg_available": False,
            "ffprobe_available": False,
            "ffmpeg_path": None,
            "ffprobe_path": None,
            "ffmpeg_version": None,
            "system": self.system,
            "hardware_acceleration": {},
        }

        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            result["ffmpeg_available"] = True
            result["ffmpeg_path"] = ffmpeg_path

            try:
                cmd = [ffmpeg_path, "-version"]
                process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
                if process.returncode == 0:
                    stdout_text = process.stdout.decode("utf-8", errors="replace")
                    version_line = stdout_text.split("\n")[0] if stdout_text else ""
                    result["ffmpeg_version"] = version_line
                    _logger.debug("FFmpeg version: %s", version_line)
                    result["hardware_acceleration"] = self.check_hardware_encoders()
            except Exception as e:
                _logger.warning("Failed to get FFmpeg version: %s", e)

        ffprobe_path = self.get_ffprobe_path()
        if ffprobe_path:
            result["ffprobe_available"] = True
            result["ffprobe_path"] = ffprobe_path

        self._cached_availability_result = result
        _logger.debug(
            "FFmpeg availability check: ffmpeg=%s, ffprobe=%s",
            result["ffmpeg_available"],
            result["ffprobe_available"],
        )
        return result


# 全局 FFmpeg 管理器实例（进程级单例，跨模块共享缓存）
ffmpeg_manager = FFmpegManager()


class VideoCompressor:
    """视频压缩处理类 - 支持自动硬件加速。"""

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        enable_hardware: bool = True,
        force_encoder: str = "",
        encoder_priority: Optional[List[str]] = None,
    ):
        self.ffmpeg_path = ffmpeg_path or ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            _logger.warning("未找到 ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = "ffmpeg"

        if not enable_hardware:
            self.recommended_encoder = "libx264"
            _logger.debug("Hardware acceleration disabled, using software: libx264")
        elif force_encoder:
            self.recommended_encoder = force_encoder
            _logger.debug("Using forced encoder: %s", force_encoder)
        else:
            self.hardware_info = ffmpeg_manager.check_hardware_encoders()
            self.recommended_encoder = self._select_best_encoder(encoder_priority or ["nvidia", "intel", "amd", "apple"])

            if self.recommended_encoder != "libx264":
                available_count = self.hardware_info.get("total_hardware_encoders", 0)
                _logger.debug("Detected %d hardware encoders, using: %s", available_count, self.recommended_encoder)
            else:
                _logger.debug("No hardware encoders available, using software: libx264")

    def _select_best_encoder(self, priority_list: List[str]) -> str:
        """根据配置的优先级选择最佳编码器。"""
        available_encoders = self.hardware_info.get("available_encoders", [])
        if not available_encoders:
            return "libx264"

        for encoder_type in priority_list:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]

        for encoder in available_encoders:
            if encoder["codec"] == "h264":
                return encoder["name"]

        return "libx264"

    def compress_video(
        self,
        input_path: str,
        output_path: str,
        target_size_mb: int = 100,
        quality: int = 23,
    ) -> bool:
        """压缩视频到指定大小。

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            target_size_mb: 目标文件大小（MB）
            quality: 压缩质量 (1-51，数值越小质量越高)

        Returns:
            是否压缩成功
        """
        try:
            if not os.path.exists(input_path):
                _logger.error("输入文件不存在: %s", input_path)
                return False

            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            _logger.info(
                "Starting video compression: input=%s, size=%.2fMB, target=%dMB, encoder=%s",
                input_path,
                input_size_mb,
                target_size_mb,
                self.recommended_encoder,
            )

            if input_size_mb <= target_size_mb:
                shutil.copy2(input_path, output_path)
                _logger.debug("File size already meets requirement, skipping compression (%.2fMB)", input_size_mb)
                return True

            cmd = self._build_compression_command(input_path, output_path, quality)
            _logger.debug("Executing FFmpeg compression: %s", " ".join(cmd))

            result = subprocess.run(cmd, capture_output=True, text=False, timeout=1800)

            if result.returncode == 0:
                if os.path.exists(output_path):
                    output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    compression_ratio = (1 - output_size_mb / input_size_mb) * 100
                    _logger.info(
                        "Video compression successful: %.2fMB -> %.2fMB (%.1f%%), encoder=%s",
                        input_size_mb,
                        output_size_mb,
                        compression_ratio,
                        self.recommended_encoder,
                    )

                    if output_size_mb > target_size_mb and quality < 35:
                        _logger.debug(
                            "Output still oversized (%.2fMB > %dMB), increasing compression",
                            output_size_mb,
                            target_size_mb,
                        )
                        return self.compress_video(input_path, output_path, target_size_mb, quality + 5)

                    return True
                _logger.error("压缩后文件不存在")
                return False

            _logger.error("视频压缩失败，返回码: %d", result.returncode)
            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", errors="replace")
                _logger.error("FFmpeg错误: %s", stderr_text)
            return False

        except subprocess.TimeoutExpired:
            _logger.error("视频压缩超时")
            return False
        except Exception as e:
            _logger.error("视频压缩异常: %s", e)
            return False

    def _build_compression_command(self, input_path: str, output_path: str, quality: int) -> List[str]:
        """构建基于硬件加速的压缩命令。"""
        cmd = [self.ffmpeg_path, "-i", input_path]

        if "nvenc" in self.recommended_encoder:
            cmd.extend([
                "-c:v", self.recommended_encoder,
                "-cq", str(quality),
                "-preset", "p4",
                "-profile:v", "high",
                "-c:a", "aac",
                "-b:a", "128k",
            ])
        elif "qsv" in self.recommended_encoder:
            cmd.extend([
                "-c:v", self.recommended_encoder,
                "-global_quality", str(quality),
                "-preset", "medium",
                "-c:a", "aac",
                "-b:a", "128k",
            ])
        elif "amf" in self.recommended_encoder:
            cmd.extend([
                "-c:v", self.recommended_encoder,
                "-qp_i", str(quality),
                "-qp_p", str(quality),
                "-quality", "balanced",
                "-c:a", "aac",
                "-b:a", "128k",
            ])
        elif "videotoolbox" in self.recommended_encoder:
            cmd.extend([
                "-c:v", self.recommended_encoder,
                "-q:v", str(quality),
                "-c:a", "aac",
                "-b:a", "128k",
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-crf", str(quality),
                "-preset", "medium",
                "-c:a", "aac",
                "-b:a", "128k",
            ])

        cmd.extend(["-movflags", "+faststart", "-y", output_path])
        return cmd
