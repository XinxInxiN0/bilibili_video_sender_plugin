# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import tempfile
import time
import urllib.parse
import urllib.request
import subprocess
import shutil
import aiohttp

from typing import Any, Dict, List, Optional, Tuple, Type

from maim_message import Seg

from src.common.logger import get_logger

# 为模块级独立函数创建logger
_utils_logger = get_logger("plugin.bilibili_video_sender.utils")


def _is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker 容器内。"""
    if os.path.exists("/.dockerenv"):
        return True
    cgroup_path = "/proc/1/cgroup"
    try:
        with open(cgroup_path, "rt", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return any(keyword in content for keyword in ("docker", "kubepods", "containerd"))
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _get_download_temp_dir() -> str:
    """获取下载临时目录：Docker 使用共享目录，非 Docker 使用系统临时目录。"""
    if _is_running_in_docker():
        return "/MaiMBot/data/tmp"
    return tempfile.gettempdir()


def convert_windows_to_wsl_path(windows_path: str) -> str:
    """将Windows路径转换为WSL路径
    
    例如：E:\\path\\to\\file.mp4 -> /mnt/e/path/to/file.mp4
    """
    try:
        # 尝试使用wslpath命令转换路径（从Windows调用WSL）
        try:
            # 在Windows上调用wsl wslpath命令
            result = subprocess.run(['wsl', 'wslpath', '-u', windows_path], 
                                   capture_output=True, text=False, check=True)
            wsl_path = result.stdout.decode('utf-8', errors='replace').strip()
            if wsl_path:
                return wsl_path
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
            
        # 如果wslpath命令失败，手动转换路径
        # 移除盘符中的冒号，将反斜杠转换为正斜杠
        if re.match(r'^[a-zA-Z]:', windows_path):
            drive = windows_path[0].lower()
            path = windows_path[2:].replace('\\', '/')
            return f"/mnt/{drive}/{path}"
        return windows_path
    except Exception:
        # 转换失败时返回原路径
        return windows_path

from src.plugin_system.base import (
    BaseAction,
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    ComponentInfo,
)
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.base.component_types import (
    ActionActivationType,
    EventType,
    MaiMessages,
)
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.apis import send_api
from src.config.config import global_config


class FFmpegManager:
    """跨平台FFmpeg管理器"""
    
    _logger = get_logger("plugin.bilibili_video_sender.ffmpeg_manager")

    def __init__(self):
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.system = platform.system().lower()
        self.ffmpeg_dir = os.path.join(self.plugin_dir, 'ffmpeg')

    def get_ffmpeg_path(self) -> Optional[str]:
        """获取ffmpeg可执行文件路径"""
        return self._get_executable_path('ffmpeg')

    def get_ffprobe_path(self) -> Optional[str]:
        """获取ffprobe可执行文件路径"""
        return self._get_executable_path('ffprobe')

    def _get_executable_path(self, executable_name: str) -> Optional[str]:
        """根据操作系统获取可执行文件路径"""
        # 确定可执行文件名称和路径
        if self.system == "windows":
            bin_dir = os.path.join(self.ffmpeg_dir, 'bin')
            executable_path = os.path.join(bin_dir, f'{executable_name}.exe')
        elif self.system in ["linux", "darwin"]:  # Linux 和 macOS
            # 优先检查平台特定的目录
            platform_bin_dir = os.path.join(self.ffmpeg_dir, 'bin', self.system)
            executable_path = os.path.join(platform_bin_dir, executable_name)

            # 如果平台特定目录不存在，检查通用bin目录
            if not os.path.exists(executable_path):
                bin_dir = os.path.join(self.ffmpeg_dir, 'bin')
                executable_path = os.path.join(bin_dir, executable_name)
        else:
            self._logger.warning(f"不支持的操作系统: {self.system}")
            return None

        # 检查插件内置的ffmpeg
        if os.path.exists(executable_path):
            self._logger.debug(f"Found bundled {executable_name}: {executable_path}")
            return executable_path

        # 检查系统PATH中的ffmpeg
        system_executable = shutil.which(executable_name)
        if system_executable:
            self._logger.debug(f"Found system {executable_name}: {system_executable}")
            return system_executable

        self._logger.warning(f"未找到{executable_name}可执行文件")
        return None

    _cached_check_result: Optional[Dict[str, Any]] = None

    def check_hardware_encoders(self) -> Dict[str, Any]:
        """检测可用的硬件编码器（带缓存）"""
        if self._cached_check_result is not None:
            return self._cached_check_result

        ffmpeg_path = self.get_ffmpeg_path()
        if not ffmpeg_path:
            return {"available_encoders": [], "recommended_encoder": "libx264"}
        
        available_encoders = []
        
        # 定义要检测的硬件编码器列表（按优先级排序）
        encoders_to_check = [
            # NVIDIA GPU 编码器
            {"name": "h264_nvenc", "type": "nvidia", "codec": "h264", "description": "NVIDIA H.264硬件编码"},
            {"name": "hevc_nvenc", "type": "nvidia", "codec": "h265", "description": "NVIDIA H.265硬件编码"},
            
            # Intel Quick Sync Video
            {"name": "h264_qsv", "type": "intel", "codec": "h264", "description": "Intel QSV H.264硬件编码"},
            {"name": "hevc_qsv", "type": "intel", "codec": "h265", "description": "Intel QSV H.265硬件编码"},
            
            # AMD GPU 编码器
            {"name": "h264_amf", "type": "amd", "codec": "h264", "description": "AMD H.264硬件编码"},
            {"name": "hevc_amf", "type": "amd", "codec": "h265", "description": "AMD H.265硬件编码"},
            
            # Apple VideoToolbox (macOS)
            {"name": "h264_videotoolbox", "type": "apple", "codec": "h264", "description": "Apple H.264硬件编码"},
            {"name": "hevc_videotoolbox", "type": "apple", "codec": "h265", "description": "Apple H.265硬件编码"},
        ]
        
        try:
            # 获取所有可用的编码器
            cmd = [ffmpeg_path, '-encoders']
            process = subprocess.run(cmd, capture_output=True, text=False, timeout=15)
            
            if process.returncode == 0:
                encoders_output = process.stdout.decode('utf-8', errors='replace')
                
                # 检查每个硬件编码器是否可用
                for encoder in encoders_to_check:
                    if encoder["name"] in encoders_output:
                        # 进一步测试编码器是否真正可用
                        if self._test_encoder(ffmpeg_path, encoder["name"]):
                            available_encoders.append(encoder)
                            self._logger.debug(f"Found available encoder: {encoder['description']}")
                        else:
                            self._logger.debug(f"Encoder {encoder['name']} exists but unavailable")
            else:
                stderr_text = process.stderr.decode('utf-8', errors='replace') if process.stderr else ''
                self._logger.warning(f"获取编码器列表失败: {stderr_text}")
                
        except Exception as e:
            self._logger.warning(f"检测硬件编码器时发生错误: {e}")
        
        # 确定推荐的编码器
        recommended_encoder = self._get_recommended_encoder(available_encoders)
        
        result = {
            "available_encoders": available_encoders,
            "recommended_encoder": recommended_encoder,
            "total_hardware_encoders": len(available_encoders)
        }
        
        self._cached_check_result = result
        self._logger.debug(f"Hardware encoder detection complete: {len(available_encoders)} available, recommend: {recommended_encoder}")
        return result
    
    def _test_encoder(self, ffmpeg_path: str, encoder_name: str) -> bool:
        """测试编码器是否真正可用"""
        try:
            # 创建一个1秒的测试视频来验证编码器
            cmd = [
                ffmpeg_path,
                '-f', 'lavfi',
                '-i', 'testsrc=duration=1:size=320x240:rate=1',
                '-c:v', encoder_name,
                '-t', '1',
                '-f', 'null',
                '-'
            ]
            
            process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
            return process.returncode == 0
            
        except Exception:
            return False
    
    def _get_recommended_encoder(self, available_encoders: List[Dict[str, Any]]) -> str:
        """根据可用编码器选择推荐的编码器"""
        if not available_encoders:
            return "libx264"  # 默认软件编码器
        
        # 优先级排序：NVIDIA > Intel > AMD > Apple
        priority_order = ["nvidia", "intel", "amd", "apple"]
        
        for encoder_type in priority_order:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]
        
        # 如果没有H.264硬件编码器，返回第一个可用的
        return available_encoders[0]["name"]

    _cached_availability_result: Optional[Dict[str, Any]] = None

    def check_ffmpeg_availability(self) -> Dict[str, Any]:
        """检查FFmpeg可用性（带缓存）"""
        if self._cached_availability_result is not None:
            return self._cached_availability_result

        result = {
            "ffmpeg_available": False,
            "ffprobe_available": False,
            "ffmpeg_path": None,
            "ffprobe_path": None,
            "ffmpeg_version": None,
            "system": self.system,
            "hardware_acceleration": {}
        }

        # 检查ffmpeg
        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            result["ffmpeg_available"] = True
            result["ffmpeg_path"] = ffmpeg_path

            try:
                # 获取ffmpeg版本信息
                cmd = [ffmpeg_path, '-version']
                process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
                if process.returncode == 0:
                    stdout_text = process.stdout.decode('utf-8', errors='replace')
                    version_line = stdout_text.split('\n')[0] if stdout_text else ""
                    result["ffmpeg_version"] = version_line
                    self._logger.debug(f"FFmpeg version: {version_line}")
                    
                    # 检测硬件编码器
                    result["hardware_acceleration"] = self.check_hardware_encoders()
            except Exception as e:
                self._logger.warning(f"Failed to get FFmpeg version: {e}")

        # 检查ffprobe
        ffprobe_path = self.get_ffprobe_path()
        if ffprobe_path:
            result["ffprobe_available"] = True
            result["ffprobe_path"] = ffprobe_path

        self._cached_availability_result = result
        self._logger.debug(f"FFmpeg availability check: ffmpeg={result['ffmpeg_available']}, ffprobe={result['ffprobe_available']}")
        return result


# 全局FFmpeg管理器实例
_ffmpeg_manager = FFmpegManager()



class ProgressBar:
    """进度条显示类"""
    
    def __init__(self, total_size: int, description: str = "下载进度", bar_length: int = 30):
        self.total_size = total_size
        self.description = description
        self.bar_length = bar_length
        self.current_size = 0
        self.last_update = 0
        self.update_interval = 0.1  # 100ms更新一次，避免过于频繁
        
    def update(self, downloaded: int):
        """更新进度"""
        self.current_size = downloaded
        current_time = time.time()
        
        # 控制更新频率，避免过于频繁的日志输出
        if current_time - self.last_update < self.update_interval:
            return
            
        self.last_update = current_time
        
        # 计算进度百分比
        if self.total_size > 0:
            percentage = (downloaded / self.total_size) * 100
        else:
            percentage = 0
            
        # 计算进度条填充长度
        filled_length = int(self.bar_length * downloaded // self.total_size) if self.total_size > 0 else 0
        
        # 构建进度条
        bar = '█' * filled_length + '░' * (self.bar_length - filled_length)
        
        # 格式化文件大小显示
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024) if self.total_size > 0 else 0
        
        # 输出进度条
        print(f"\r{self.description}: [{bar}] {percentage:5.1f}% ({downloaded_mb:6.1f}MB/{total_mb:6.1f}MB)", end='', flush=True)
        
    def finish(self):
        """完成进度条显示"""
        # 确保显示100%
        self.update(self.total_size)
        print()  # 换行


class BilibiliVideoInfo:
    """基础视频信息。"""
    
    def __init__(self, aid: int, cid: int, title: str, bvid: Optional[str] = None, duration: Optional[int] = None):
        self.aid = aid
        self.cid = cid
        self.title = title
        self.bvid = bvid
        self.duration = duration


class BilibiliParser:
    """哔哩哔哩链接解析器。"""
    
    _logger = get_logger("plugin.bilibili_video_sender.parser")

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/144.0.0.0 Safari/537.36"
    )

    # 允许匹配携带查询参数的链接（用于保留 ?p= 分P 信息）
    VIDEO_URL_PATTERN = re.compile(
        r"https?://(?:(?:www|m)\.)?bilibili\.com/video/(?P<bv>BV[\w]+|av\d+)(?:/)?(?:\?[^\s#]+)?",
        re.IGNORECASE,
    )
    B23_SHORT_PATTERN = re.compile(
        r"https?://b23\.tv/[\w]+(?:\?[^\s#]+)?",
        re.IGNORECASE,
    )
    QN_TEXT_PATTERN = re.compile(r"(?:[?&]|\b)qn\s*=\s*(\d+)", re.IGNORECASE)
    QN_INFO = {
        # 6: "240P 极速", # 仅 MP4 格式支持，仅 `platform=html5` 时有效
        16: "360P 流畅",
        32: "480P 清晰",
        64: "720P 高清", # WEB 端默认值，无 720P 时则为 720P60
        74: "720P60 高帧率", # 登录认证
        80: "1080P 高清", # TV 端与 APP 端默认值，登录认证
        # 100: "智能修复", # 人工智能增强画质，大会员认证
        112: "1080P+ 高码率", # 大会员认证
        116: "1080P60 高帧率", # 大会员认证
        120: "4K 超清", # 需要 `fnval&128=128` 且 `fourk=1`，大会员认证
        125: "HDR 真彩色", # 仅支持 DASH 格式，需要 `fnval&64=64`，大会员认证
        126: "杜比视界", # 仅支持 DASH 格式，需要 `fnval&512=512`，大会员认证
        127: "8K 超高清", # 仅支持 DASH 格式，需要 `fnval&1024=1024`，大会员认证
        # 129: "HDR Vivid", # 大会员认证
    }

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """清理 URL 尾部可能出现的标点符号，避免解析失败。"""
        return url.rstrip(").,，。!?》】】〕」\"'") if url else url

    @staticmethod
    def _extract_qn_from_text(text: str) -> Optional[int]:
        """从原始文本中解析 qn 参数，作为 URL 提取丢参时的兜底。"""
        if not text:
            return None
        match = BilibiliParser.QN_TEXT_PATTERN.search(text)
        if not match:
            return None
        return BilibiliParser._safe_int(match.group(1), 0) or None

    @staticmethod
    def _extract_page_param(url: str) -> int:
        """解析分P参数 p，默认为 1。"""
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query or "")
            p_raw = qs.get("p", [None])[0]
            p_val = BilibiliParser._safe_int(p_raw, 1)
            return p_val if p_val > 0 else 1
        except Exception:
            return 1

    @staticmethod
    def _extract_qn_param(url: str) -> Optional[int]:
        """解析清晰度参数 qn，返回 None 表示未指定。"""
        if not url:
            return None
        try:
            parsed = urllib.parse.urlparse(BilibiliParser._sanitize_url(url))
            qs = urllib.parse.parse_qs(parsed.query or "")
            qn_raw = qs.get("qn", [None])[0]
            if qn_raw is None:
                return None
            qn_val = BilibiliParser._safe_int(qn_raw, 0)
            return qn_val if qn_val > 0 else None
        except Exception:
            return None

    @staticmethod
    def _normalize_stream_urls(primary: Optional[str], backups: Optional[List[str]] = None) -> List[str]:
        """合并主链与备链，去重并统一为 https。"""
        urls: List[str] = []
        if primary:
            urls.append(primary)
        if backups:
            for item in backups:
                if item:
                    urls.append(item)
        # 去重并统一协议
        normalized: List[str] = []
        for u in urls:
            u2 = u.replace("http:", "https:")
            if u2 not in normalized:
                normalized.append(u2)
        return normalized

    @staticmethod
    def _get_qn_name(qn: int) -> str:
        return BilibiliParser.QN_INFO.get(qn, f"未知({qn})")

    @staticmethod
    def _codec_rank(codecs: str) -> int:
        if not codecs:
            return 3
        codec_lower = codecs.lower()
        if "avc" in codec_lower or "h264" in codec_lower:
            return 0
        if "hev" in codec_lower or "hvc" in codec_lower or "hevc" in codec_lower:
            return 1
        if "av01" in codec_lower:
            return 2
        return 3

    @staticmethod
    def _select_video_stream(
        videos: List[Dict[str, Any]],
        target_qn: int,
        strict_qn: bool,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int], str]:
        if not videos:
            return None, None, "no_video"

        eligible = videos
        if target_qn > 0:
            if strict_qn:
                eligible = [
                    video for video in videos
                    if BilibiliParser._safe_int(video.get("id")) == target_qn
                ]
                if not eligible:
                    return None, None, "strict_no_match"
            else:
                eligible = [
                    video for video in videos
                    if BilibiliParser._safe_int(video.get("id")) <= target_qn
                ]
        else:
            eligible = list(videos)

        if not eligible:
            if strict_qn:
                return None, None, "strict_no_match"
            eligible = list(videos)
            fallback = True
        else:
            fallback = False

        if strict_qn and target_qn > 0:
            candidates = list(eligible)
        else:
            best_id = max(
                (BilibiliParser._safe_int(video.get("id")) for video in eligible),
                default=0,
            )
            if best_id > 0:
                candidates = [
                    video for video in eligible
                    if BilibiliParser._safe_int(video.get("id")) == best_id
                ]
            else:
                candidates = list(eligible)

        candidates.sort(
            key=lambda video: (
                BilibiliParser._codec_rank(str(video.get("codecs", ""))),
                -BilibiliParser._safe_int(video.get("bandwidth")),
            )
        )
        best_video = candidates[0] if candidates else None
        selected_qn = BilibiliParser._safe_int(best_video.get("id")) if best_video else None
        return best_video, selected_qn, "fallback" if fallback else "ok"

    @staticmethod
    def _build_request(url: str, headers: Optional[Dict[str, str]] = None) -> urllib.request.Request:
        default_headers = {
            "User-Agent": BilibiliParser.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        if headers:
            default_headers.update(headers)
        return urllib.request.Request(url, headers=default_headers)

    @staticmethod
    def _fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发送 HTTP 请求并解析 JSON。

        说明：
        - 默认会带上 User-Agent / Referer。
        - 当需要登录鉴权时，可通过 headers 传入 Cookie。
        """
        req = BilibiliParser._build_request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data = resp.read()
        return json.loads(data.decode("utf-8", errors="ignore"))

    @staticmethod
    def _follow_redirect(url: str) -> str:
        # 使用 curl UA，可以有效避免在服务器上发送网络请求被 412（经测试，BilibiliParser.USER_AGENT 也可能会被封控）
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public short URL
            return resp.geturl()

    @staticmethod
    def _extract_bvid(url: str) -> Optional[str]:
        match = BilibiliParser.VIDEO_URL_PATTERN.search(url)
        if not match:
            return None
        raw_id = match.group("bv")
        if raw_id.lower().startswith("bv"):
            return raw_id
        # 兼容 av 号：需要先通过 view 接口查询 bvid
        return None

    @staticmethod
    def find_first_bilibili_url(text: str) -> Optional[str]:
        # 先匹配 b23.tv 短链
        short = BilibiliParser.B23_SHORT_PATTERN.search(text)
        if short:
            # 直接返回短链接，在异步任务中解析跳转
            return BilibiliParser._sanitize_url(short.group(0))

        # 再匹配标准视频链接
        match = BilibiliParser.VIDEO_URL_PATTERN.search(text)
        if match:
            return BilibiliParser._sanitize_url(match.group(0))
        return None

    @staticmethod
    def get_view_info_by_url(
        url: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Optional[BilibiliVideoInfo]:
        # 优先解析 BV 号
        bvid = BilibiliParser._extract_bvid(url)

        # 解析分 P 参数（默认P1）
        page_index = BilibiliParser._extract_page_param(url)

        # 准备可选 Cookie（用于登录可见视频）
        opts = options or {}
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)

        query: str
        if bvid:
            query = f"bvid={urllib.parse.quote(bvid)}"
        else:
            # 兜底：尝试从路径中提取 av 号
            m = re.search(r"/video/av(?P<aid>\d+)", url)
            if not m:
                return None
            aid = m.group("aid")
            query = f"aid={aid}"

        api = f"https://api.bilibili.com/x/web-interface/view?{query}"
        payload = BilibiliParser._fetch_json(api, headers=headers)
        if payload.get("code") != 0:
            return None

        data = payload.get("data", {})
        pages = data.get("pages") or []
        if not pages:
            return None

        # 按 p 参数选择分 P，超出范围时回退到 P1
        if page_index > len(pages):
            BilibiliParser._logger.warning(
                f"分P参数超出范围: p={page_index}, total={len(pages)}，回退到 P1"
            )
            page_index = 1

        selected_page = pages[page_index - 1]
        page_duration = selected_page.get("duration")

        return BilibiliVideoInfo(
            aid=int(data.get("aid")),
            cid=int(selected_page.get("cid")),
            title=str(data.get("title", "")),
            bvid=str(data.get("bvid", "")) or None,
            duration=page_duration if page_duration is not None else data.get("duration"),
        )

    @staticmethod
    def get_play_urls(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        opts = options or {}
        
        # 配置参数
        BilibiliParser._logger.debug("Starting to fetch video playback URLs", aid=aid, cid=cid)
        
        # 硬编码配置项
        use_wbi = True
        prefer_dash = True
        fnval = 4048
        qn = 0
        platform = "pc"
        high_quality = 0  # false -> 0
        try_look = 0  # false -> 0
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        requested_qn = BilibiliParser._safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0
        
        # 鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)
        
        if not has_cookie:
            BilibiliParser._logger.warning("未提供Cookie，将使用游客模式（清晰度限制）")
        
        # 清晰度选择逻辑优化
        if requested_qn == 0:
            qn = 64 if has_cookie else 32
            BilibiliParser._logger.debug(f"Auto quality enabled: effective_qn={qn}")
        else:
            qn = requested_qn

        fourk = 1 if qn >= 120 else 0

        qn_name = BilibiliParser._get_qn_name(qn)
        if qn >= 64 and not has_cookie:
            BilibiliParser._logger.warning(f"请求{qn_name}清晰度但未登录，可能失败")
        if qn >= 80 and not has_cookie:
            BilibiliParser._logger.warning(f"请求{qn_name}清晰度需要大会员账号")
        if qn >= 116 and not has_cookie:
            BilibiliParser._logger.warning(f"请求{qn_name}高帧率需要大会员账号")
        if qn >= 125 and not has_cookie:
            BilibiliParser._logger.warning(f"请求{qn_name}需要大会员账号")

        opts["requested_qn"] = requested_qn
        opts["effective_qn"] = qn
        opts["qn_strict"] = strict_qn

        # 构建请求参数
        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fnver": "0",
            "fnval": str(fnval),
            "fourk": str(fourk),
            "platform": platform,
        }
        
        if qn > 0:
            params["qn"] = str(qn)
            
        if high_quality:
            params["high_quality"] = "1"
            
        if try_look:
            params["try_look"] = "1"
            
        if buvid3:
            # 生成 session: md5(buvid3 + 当前毫秒)
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash
            
        # 添加gaia_source参数（有Cookie时非必要）
        if not has_cookie:
            params["gaia_source"] = "view-card"

        # WBI 签名
        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )

        if use_wbi:
            try:
                final_params = BilibiliWbiSigner.sign_params(params)
            except Exception as e:
                BilibiliParser._logger.warning(f"WBI 签名失败，降级到非 WBI 接口: {e}")
                api_base = "https://api.bilibili.com/x/player/playurl"
                final_params = params
        else:
            final_params = params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        # 构建请求头：可带 Cookie
        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)
        else:
            BilibiliParser._logger.info("使用游客模式")

        # 发起请求
        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            BilibiliParser._logger.error(f"HTTP请求失败: {e}")
            return None, f"网络请求失败: {e}"
            
        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            BilibiliParser._logger.error(f"JSON解析失败: {e}")
            return None, "响应数据格式错误"
            
        if payload.get("code") != 0:
            error_msg = payload.get("message", "接口返回错误")
            BilibiliParser._logger.error(f"API返回错误: code={payload.get('code')}, message={error_msg}")
            return None, error_msg

        BilibiliParser._logger.debug("API请求成功，开始解析响应数据")
        data = payload.get("data", {})

        # 处理dash格式
        dash = data.get("dash")
        if not dash:
            BilibiliParser._logger.debug("未找到dash格式数据")
            # 检查是否有durl格式
            durl = data.get("durl") or []
            if durl:
                BilibiliParser._logger.debug(f"找到durl格式数据，共{len(durl)}个文件")
                # durl 可能为分段视频，目前仅处理第一段
                if len(durl) > 1:
                    BilibiliParser._logger.warning(
                        f"durl为多段视频（{len(durl)}段），当前仅处理第一段"
                    )
                item = durl[0]
                # durl 可能带 backup_url，统一合并后交给下载阶段
                primary = item.get("url") or item.get("baseUrl") or item.get("base_url")
                backups = item.get("backup_url") or item.get("backupUrl") or []
                urls = BilibiliParser._normalize_stream_urls(primary, backups)
                if urls:
                    return {"type": "durl", "urls": urls}, "ok (durl格式)"
            return None, "未找到dash数据"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        BilibiliParser._logger.debug(f"找到{len(videos)}个视频流和{len(audios)}个音频流")
        
        # 记录视频流详细信息
        if videos:
            BilibiliParser._logger.debug("Video stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Resolution':<12} {'Codec':<25} {'Bitrate':<10} {'FPS':<10}")
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                BilibiliParser._logger.debug(f"{i+1:<4} {width}x{height:<8} {codec:<25} {bandwidth//1000:<10}kbps {frame_rate:<10}")
        
        # 记录音频流详细信息
        if audios:
            BilibiliParser._logger.debug("Audio stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            BilibiliParser._logger.debug(f"Found {len(dolby_audios)} Dolby audio streams")
            if dolby_audios:
                BilibiliParser._logger.debug("Dolby audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            BilibiliParser._logger.debug(f"Found {len(flac_audios)} FLAC audio stream")
            if flac_audios:
                BilibiliParser._logger.debug("FLAC audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        # 合并所有音频流
        all_audios = audios + dolby_audios + flac_audios
        
        if not videos:
            BilibiliParser._logger.warning("未找到视频流")
            return None, "未找到视频流"
            
        if not all_audios:
            BilibiliParser._logger.warning("未找到音频流")
        
        # 参考原脚本，按照质量排序（降序）
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        
        
        # 选择符合清晰度与编码偏好的视频流
        best_video, selected_qn, selection_status = BilibiliParser._select_video_stream(
            videos, qn, strict_qn
        )
        if not best_video:
            if selection_status == "strict_no_match":
                requested_name = BilibiliParser._get_qn_name(requested_qn)
                return None, f"请求清晰度不可用: {requested_name}"
            BilibiliParser._logger.error("Failed to select video stream")
            return None, "未获取到播放地址"

        if selected_qn is not None:
            selected_name = BilibiliParser._get_qn_name(selected_qn)
            opts["selected_qn"] = selected_qn
            opts["selected_qn_name"] = selected_name
            if requested_qn:
                requested_name = BilibiliParser._get_qn_name(requested_qn)
                opts["requested_qn_name"] = requested_name
            else:
                opts["requested_qn_name"] = "自动"

            if requested_qn != 0 and selected_qn != requested_qn:
                BilibiliParser._logger.info(
                    f"Quality downgrade: requested {requested_name} (qn={requested_qn}), "
                    f"selected {selected_name} (qn={selected_qn})"
                )
            else:
                BilibiliParser._logger.info(
                    f"Quality selected: requested_qn={requested_qn}, selected_qn={selected_qn}"
                )

        if selection_status == "fallback":
            BilibiliParser._logger.info(
                f"No eligible streams for qn={qn}, fell back to best available stream"
            )

        # 将 baseUrl 与 backupUrl 合并成优先列表，下载阶段按序尝试
        video_url = best_video.get("baseUrl") or best_video.get("base_url")
        video_backups = best_video.get("backupUrl") or best_video.get("backup_url") or []
        video_urls = BilibiliParser._normalize_stream_urls(video_url, video_backups)

        if video_url:
            codec = best_video.get("codecs", "unknown")
            bandwidth = best_video.get("bandwidth", 0)
            width = best_video.get("width", 0)
            height = best_video.get("height", 0)
            BilibiliParser._logger.debug(
                f"Selected best video stream: {width}x{height}, {codec}, {bandwidth//1000}kbps"
            )

        audio_urls: List[str] = []
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            audio_backups = best_audio.get("backupUrl") or best_audio.get("backup_url") or []
            audio_urls = BilibiliParser._normalize_stream_urls(audio_url, audio_backups)
            if audio_url:
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"Selected best audio stream: {codec}, {bandwidth//1000}kbps")

        if video_urls:
            return {"type": "dash", "video_urls": video_urls, "audio_urls": audio_urls}, "ok"

        BilibiliParser._logger.error("Failed to get playback URLs")
        return None, "未获取到播放地址"
    
    @staticmethod
    def get_play_urls_force_dash(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """强制获取dash格式的视频和音频流"""
        opts = options or {}
        
        BilibiliParser._logger.debug(f"=== Force fetch DASH format ===")
        BilibiliParser._logger.debug(f"Video ID: aid={aid}, cid={cid}")
        BilibiliParser._logger.debug(f"Config: {opts}")
        
        # 硬编码配置项
        use_wbi = True
        fnval = 4048  # 强制使用DASH格式
        platform = "pc"
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        requested_qn = BilibiliParser._safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0
        
        # 记录鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)
        BilibiliParser._logger.debug(f"Force DASH auth: has_cookie={has_cookie}, has_buvid3={has_buvid3}")
        
        if not has_cookie:
            BilibiliParser._logger.warning("Force DASH: no Cookie, may affect HD fetching")

        if requested_qn == 0:
            qn = 64 if has_cookie else 32
            BilibiliParser._logger.debug(f"Force DASH auto quality: effective_qn={qn}")
        else:
            qn = requested_qn

        fourk = 1 if qn >= 120 else 0

        qn_name = BilibiliParser._get_qn_name(qn)
        if qn >= 64 and not has_cookie:
            BilibiliParser._logger.warning(f"Force DASH: 请求{qn_name}清晰度但未登录，可能失败")
        if qn >= 80 and not has_cookie:
            BilibiliParser._logger.warning(f"Force DASH: 请求{qn_name}清晰度需要大会员账号")
        if qn >= 116 and not has_cookie:
            BilibiliParser._logger.warning(f"Force DASH: 请求{qn_name}高帧率需要大会员账号")
        if qn >= 125 and not has_cookie:
            BilibiliParser._logger.warning(f"Force DASH: 请求{qn_name}需要大会员账号")

        opts["requested_qn"] = requested_qn
        opts["effective_qn"] = qn
        opts["qn_strict"] = strict_qn
        
        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fourk": str(fourk),
            "fnver": "0",
            "fnval": str(fnval),
            "platform": platform,
        }

        if qn > 0:
            params["qn"] = str(qn)
        
        if buvid3:
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash
            
        # 添加gaia_source参数（有Cookie时非必要）
        if not has_cookie:
            params["gaia_source"] = "view-card"

        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )

        if use_wbi:
            try:
                final_params = BilibiliWbiSigner.sign_params(params)
            except Exception as e:
                BilibiliParser._logger.warning(f"Force DASH: WBI签名失败，降级到非WBI接口: {e}")
                api_base = "https://api.bilibili.com/x/player/playurl"
                final_params = params
        else:
            final_params = params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)

        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            BilibiliParser._logger.error(f"Force DASH HTTP error: {e}")
            return None, f"Force DASH network error: {e}"
            
        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            BilibiliParser._logger.error(f"Force DASH JSON parse error: {e}")
            return None, "Force DASH response format error"
            
        if payload.get("code") != 0:
            error_msg = payload.get("message", "API error")
            BilibiliParser._logger.error(f"Force DASH API error: code={payload.get('code')}, msg={error_msg}")
            return None, error_msg

        BilibiliParser._logger.debug("Force DASH request successful, parsing response")
        data = payload.get("data", {})
        
        # 检查是否仍然返回durl格式
        durl = data.get("durl") or []
        if durl:
            BilibiliParser._logger.debug(f"Force DASH also returned durl format: {len(durl)} files (single-file only)")
            if len(durl) > 1:
                BilibiliParser._logger.warning(f"Force DASH: durl为多段视频（{len(durl)}段），当前仅处理第一段")
            item = durl[0]
            # durl 可能带 backup_url，统一合并后交给下载阶段
            primary = item.get("url") or item.get("baseUrl") or item.get("base_url")
            backups = item.get("backup_url") or item.get("backupUrl") or []
            urls = BilibiliParser._normalize_stream_urls(primary, backups)
            if urls:
                return {"type": "durl", "urls": urls}, "ok (durl格式)"
        
        dash = data.get("dash")
        if not dash:
            BilibiliParser._logger.warning("Force DASH: no dash data found")
            # 检查其他可能的数据结构
            BilibiliParser._logger.info(f"Force DASH response data structure: {list(data.keys())}")
            return None, "No dash data"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        BilibiliParser._logger.debug(f"Force DASH: {len(videos)} video streams, {len(audios)} audio streams")
        
        # 记录视频流详细信息（表格格式）
        if videos:
            BilibiliParser._logger.debug("Force DASH video stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Resolution':<12} {'Codec':<25} {'Bitrate':<10} {'FPS':<10}")
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                BilibiliParser._logger.debug(f"{i+1:<4} {width}x{height:<8} {codec:<25} {bandwidth//1000:<10}kbps {frame_rate:<10}")
        
        # 记录音频流详细信息（表格格式）
        if audios:
            BilibiliParser._logger.debug("Force DASH audio stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            if dolby_audios:
                BilibiliParser._logger.debug("Force DASH Dolby audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            if flac_audios:
                BilibiliParser._logger.debug("Force DASH FLAC audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
        
        all_audios = audios + dolby_audios + flac_audios
        
        if not videos or not all_audios:
            BilibiliParser._logger.warning(f"Force DASH: missing streams - video={len(videos)}, audio={len(all_audios)}")
            return None, "Missing video or audio streams"
        
        # 按照质量排序
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        
        
        # 获取符合清晰度与编码偏好的视频流
        best_video, selected_qn, selection_status = BilibiliParser._select_video_stream(
            videos, qn, strict_qn
        )
        if not best_video:
            if selection_status == "strict_no_match":
                requested_name = BilibiliParser._get_qn_name(requested_qn)
                return None, f"Force DASH: 请求清晰度不可用: {requested_name}"
            return None, "Force DASH: missing video stream"

        if selected_qn is not None:
            selected_name = BilibiliParser._get_qn_name(selected_qn)
            opts["selected_qn"] = selected_qn
            opts["selected_qn_name"] = selected_name
            if requested_qn:
                requested_name = BilibiliParser._get_qn_name(requested_qn)
                opts["requested_qn_name"] = requested_name
            else:
                opts["requested_qn_name"] = "自动"

            if requested_qn != 0 and selected_qn != requested_qn:
                BilibiliParser._logger.info(
                    f"Force DASH quality downgrade: requested {requested_name} (qn={requested_qn}), "
                    f"selected {selected_name} (qn={selected_qn})"
                )
            else:
                BilibiliParser._logger.info(
                    f"Force DASH quality selected: requested_qn={requested_qn}, selected_qn={selected_qn}"
                )

        if selection_status == "fallback":
            BilibiliParser._logger.info(
                f"Force DASH: no eligible streams for qn={qn}, fell back to best available stream"
            )

        # 将 baseUrl 与 backupUrl 合并成优先列表，下载阶段按序尝试
        video_url = best_video.get("baseUrl") or best_video.get("base_url")
        video_backups = best_video.get("backupUrl") or best_video.get("backup_url") or []
        video_urls = BilibiliParser._normalize_stream_urls(video_url, video_backups)

        if video_url:
            codec = best_video.get("codecs", "unknown")
            bandwidth = best_video.get("bandwidth", 0)
            width = best_video.get("width", 0)
            height = best_video.get("height", 0)
            BilibiliParser._logger.debug(
                f"Force DASH selected video: {width}x{height}, {codec}, {bandwidth//1000}kbps"
            )

        audio_urls: List[str] = []
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            audio_backups = best_audio.get("backupUrl") or best_audio.get("backup_url") or []
            audio_urls = BilibiliParser._normalize_stream_urls(audio_url, audio_backups)
            if audio_url:
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"Force DASH selected audio: {codec}, {bandwidth//1000}kbps")

        if video_urls:
            return {"type": "dash", "video_urls": video_urls, "audio_urls": audio_urls}, "ok"

        return None, "Force DASH: no usable streams"

    @staticmethod
    def validate_config(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """验证配置参数的有效性"""
        
        opts = options or {}
        validation_result = {
            "valid": True,
            "warnings": [],
            "errors": [],
            "recommendations": []
        }
        

        
        # 检查Cookie配置
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        
        if not sessdata:
            validation_result["warnings"].append("未配置SESSDATA，将使用游客模式")
            validation_result["recommendations"].append("建议配置SESSDATA以获得更好的清晰度和功能")
        else:
            if len(sessdata) < 10:
                validation_result["errors"].append("SESSDATA长度异常，可能配置错误")
                validation_result["valid"] = False
                
        if not buvid3:
            # buvid3 为可选项，仅影响 session 参数生成
            validation_result["recommendations"].append("未配置Buvid3（可选），如需生成session参数可补充")
        else:
            if len(buvid3) < 10:
                validation_result["warnings"].append("Buvid3长度异常，可能配置错误（非必填）")

        
        # 检查清晰度配置
        requested_qn = BilibiliParser._safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0
        if requested_qn == 0:
            effective_qn = 64 if sessdata else 32
            qn_name = BilibiliParser._get_qn_name(effective_qn)
            BilibiliParser._logger.info(f"清晰度配置: 自动({qn_name}) (qn=0)")
        else:
            effective_qn = requested_qn
            qn_name = BilibiliParser._get_qn_name(requested_qn)
            if requested_qn not in BilibiliParser.QN_INFO:
                validation_result["warnings"].append(f"qn={requested_qn} 不在常见清晰度列表，可能无效")
            BilibiliParser._logger.info(f"清晰度配置: {qn_name} (qn={requested_qn}, strict={strict_qn})")

        if effective_qn >= 64 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}清晰度但未配置Cookie，可能失败")
        if effective_qn >= 80 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}清晰度需要大会员账号")
        if effective_qn >= 116 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}高帧率需要大会员账号")
        if effective_qn >= 125 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}需要大会员账号")
        
        # 检查其他配置（使用硬编码值）
        fnval = 4048  # 硬编码值
            
        platform = "pc"  # 硬编码值
        if platform not in ["pc", "html5"]:
            validation_result["warnings"].append(f"platform值{platform}不是标准值")
            
        # 记录验证结果
        if validation_result["warnings"]:
            BilibiliParser._logger.debug(f"Config warnings: {validation_result['warnings']}")
        if validation_result["errors"]:
            BilibiliParser._logger.error(f"Config errors: {validation_result['errors']}")
        if validation_result["recommendations"]:
            BilibiliParser._logger.debug(f"Config suggestions: {validation_result['recommendations']}")
            
        BilibiliParser._logger.debug(f"Config validation: {'pass' if validation_result['valid'] else 'fail'}")
        return validation_result

    @staticmethod
    def get_video_duration(video_path: str) -> Optional[float]:
        """获取视频时长（秒）"""
        try:
            import subprocess

            # 使用跨平台FFmpeg管理器获取ffprobe路径
            ffprobe_path = _ffmpeg_manager.get_ffprobe_path()

            if not ffprobe_path:
                BilibiliParser._logger.warning("未找到ffprobe，无法获取视频时长")
                return None

            # 使用ffprobe获取视频时长
            cmd = [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
            BilibiliParser._logger.debug(f"Running ffprobe: {' '.join(cmd)}")

            # 使用正确的编码设置来避免跨平台编码问题
            result = subprocess.run(cmd, capture_output=True, text=False)

            BilibiliParser._logger.debug(f"ffprobe return code: {result.returncode}")
            if result.stdout:
                stdout_text = result.stdout.decode('utf-8', errors='replace').strip()
                BilibiliParser._logger.debug(f"ffprobe output: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.decode('utf-8', errors='replace').strip()
                BilibiliParser._logger.debug(f"ffprobe stderr: {stderr_text}")

            if result.returncode == 0:
                duration_str = result.stdout.decode('utf-8', errors='replace').strip()
                try:
                    duration = float(duration_str)
                    BilibiliParser._logger.debug(f"Video duration: {duration}s")
                    return duration
                except ValueError:
                    BilibiliParser._logger.warning(f"Failed to parse duration: '{duration_str}'")
                    return None
            else:
                BilibiliParser._logger.warning(f"ffprobe failed with code: {result.returncode}")
                return None
        except Exception as e:
            BilibiliParser._logger.error(f"Error getting video duration: {e}")
            return None


class VideoCompressor:
    """视频压缩处理类 - 支持自动硬件加速"""
    
    _logger = get_logger("plugin.bilibili_video_sender.compressor")
    
    def __init__(self, ffmpeg_path: Optional[str] = None, config: Optional[Dict] = None):
        self.ffmpeg_path = ffmpeg_path or _ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            self._logger.warning("未找到ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = 'ffmpeg'
        
        
        # 读取配置
        self.config = config or {}
        enable_hardware = self.config.get("ffmpeg", {}).get("enable_hardware_acceleration", True)
        force_encoder = self.config.get("ffmpeg", {}).get("force_encoder", "")
        
        if not enable_hardware:
            # 禁用硬件加速
            self.recommended_encoder = "libx264"
            self._logger.debug("Hardware acceleration disabled, using software: libx264")
        elif force_encoder:
            # 强制使用指定编码器
            self.recommended_encoder = force_encoder
            self._logger.debug(f"Using forced encoder: {force_encoder}")
        else:
            # 自动检测硬件编码器
            self.hardware_info = _ffmpeg_manager.check_hardware_encoders()
            self.recommended_encoder = self._select_best_encoder()
            
            if self.recommended_encoder != "libx264":
                available_count = self.hardware_info.get("total_hardware_encoders", 0)
                self._logger.debug(f"Detected {available_count} hardware encoders, using: {self.recommended_encoder}")
            else:
                self._logger.debug("No hardware encoders available, using software: libx264")
    
    def _select_best_encoder(self) -> str:
        """根据配置的优先级选择最佳编码器"""
        available_encoders = self.hardware_info.get("available_encoders", [])
        if not available_encoders:
            return "libx264"
        
        # 获取优先级配置
        priority_list = self.config.get("ffmpeg", {}).get("encoder_priority", ["nvidia", "intel", "amd", "apple"])
        
        # 按优先级查找可用的编码器
        for encoder_type in priority_list:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]
        
        # 如果按优先级没找到，返回第一个可用的H.264编码器
        for encoder in available_encoders:
            if encoder["codec"] == "h264":
                return encoder["name"]
        
        # 最后回退到软件编码
        return "libx264"
    
    def compress_video(self, input_path: str, output_path: str, target_size_mb: int = 100, quality: int = 23) -> bool:
        """
        压缩视频到指定大小
        
        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            target_size_mb: 目标文件大小（MB）
            quality: 压缩质量 (1-51，数值越小质量越高)
            
        Returns:
            是否压缩成功
        """
        try:
            import subprocess
            import os
            
            
            # 检查输入文件
            if not os.path.exists(input_path):
                self._logger.error(f"输入文件不存在: {input_path}")
                return False
            
            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            self._logger.info("Starting video compression", 
                            input_path=input_path, 
                            input_size_mb=f"{input_size_mb:.2f}", 
                            target_size_mb=target_size_mb,
                            encoder=self.recommended_encoder)
            
            # 如果文件已经小于目标大小，直接复制
            if input_size_mb <= target_size_mb:
                import shutil
                shutil.copy2(input_path, output_path)
                self._logger.debug("File size already meets requirement, skipping compression", size_mb=f"{input_size_mb:.2f}")
                return True
            
            # 构建FFmpeg压缩命令 - 使用自动检测的编码器
            cmd = self._build_compression_command(input_path, output_path, quality)
            
            self._logger.debug(f"Executing FFmpeg compression command: {' '.join(cmd)}")
            
            # 执行压缩
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=1800)  # 30分钟超时
            
            if result.returncode == 0:
                # 检查压缩后的文件大小
                if os.path.exists(output_path):
                    output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    compression_ratio = (1 - output_size_mb / input_size_mb) * 100
                    self._logger.info("Video compression successful", 
                                    input_size_mb=f"{input_size_mb:.2f}",
                                    output_size_mb=f"{output_size_mb:.2f}",
                                    compression_ratio=f"{compression_ratio:.1f}%",
                                    encoder=self.recommended_encoder)
                    
                    # 如果压缩后仍然过大，尝试更高的压缩率
                    if output_size_mb > target_size_mb and quality < 35:
                        self._logger.debug("Output still oversized, increasing compression", 
                                           output_size_mb=f"{output_size_mb:.2f}",
                                           target_size_mb=target_size_mb,
                                           new_quality=quality + 5)
                        return self.compress_video(input_path, output_path, target_size_mb, quality + 5)
                    
                    return True
                else:
                    self._logger.error("压缩后文件不存在")
                    return False
            else:
                self._logger.error(f"视频压缩失败，返回码: {result.returncode}")
                if result.stderr:
                    stderr_text = result.stderr.decode('utf-8', errors='replace')
                    self._logger.error(f"FFmpeg错误信息: {stderr_text}")
                return False
                
        except subprocess.TimeoutExpired:
            self._logger.error("视频压缩超时")
            return False
        except Exception as e:
            self._logger.error(f"视频压缩异常: {e}")
            return False
    
    def _build_compression_command(self, input_path: str, output_path: str, quality: int) -> List[str]:
        """构建基于硬件加速的压缩命令"""
        
        # 基础命令
        cmd = [self.ffmpeg_path, '-i', input_path]
        
        # 根据编码器类型添加不同的参数
        if self.recommended_encoder == "libx264":
            # 软件编码 H.264
            cmd.extend([
                '-c:v', 'libx264',
                '-crf', str(quality),
                '-preset', 'medium',
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
            self._logger.debug("使用软件编码器 libx264")
            
        elif "nvenc" in self.recommended_encoder:
            # NVIDIA 硬件编码
            cmd.extend([
                '-c:v', self.recommended_encoder,
                '-cq', str(quality),  # 对于 nvenc 使用 -cq 而不是 -crf
                '-preset', 'p4',      # NVENC 预设：p1(fastest) 到 p7(slowest)，p4是平衡
                '-profile:v', 'high',
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
            self._logger.debug(f"使用 NVIDIA 硬件编码器 {self.recommended_encoder}")
            
        elif "qsv" in self.recommended_encoder:
            # Intel Quick Sync Video
            cmd.extend([
                '-c:v', self.recommended_encoder,
                '-global_quality', str(quality),  # QSV 使用 global_quality
                '-preset', 'medium',
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
            self._logger.debug(f"使用 Intel QSV 硬件编码器 {self.recommended_encoder}")
            
        elif "amf" in self.recommended_encoder:
            # AMD 硬件编码
            cmd.extend([
                '-c:v', self.recommended_encoder,
                '-qp_i', str(quality),  # AMD AMF 使用 qp_i
                '-qp_p', str(quality),
                '-quality', 'balanced',
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
            self._logger.debug(f"使用 AMD 硬件编码器 {self.recommended_encoder}")
            
        elif "videotoolbox" in self.recommended_encoder:
            # Apple VideoToolbox
            cmd.extend([
                '-c:v', self.recommended_encoder,
                '-q:v', str(quality),  # VideoToolbox 使用 -q:v
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
            self._logger.debug(f"使用 Apple VideoToolbox 硬件编码器 {self.recommended_encoder}")
            
        else:
            # 未知编码器，回退到软件编码
            self._logger.warning(f"未知编码器 {self.recommended_encoder}，回退到软件编码")
            cmd.extend([
                '-c:v', 'libx264',
                '-crf', str(quality),
                '-preset', 'medium',
                '-c:a', 'aac',
                '-b:a', '128k'
            ])
        
        # 通用参数
        cmd.extend([
            '-movflags', '+faststart',  # 优化流媒体播放
            '-y',                       # 覆盖输出文件
            output_path
        ])
        
        return cmd



class BilibiliWbiSigner:
    """WBI 签名工具：自动获取 wbi key 并缓存，生成 w_rid/wts"""
    
    _logger = get_logger("plugin.bilibili_video_sender.wbi_signer")

    # WBI mixin key 索引表（官方 WBI 签名，长度 64）
    _mixin_key_indices: List[int] = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]

    _cached_mixin_key: Optional[str] = None
    _cached_at: float = 0.0
    _cache_ttl_seconds: int = 3600

    @classmethod
    def _fetch_wbi_keys(cls) -> Tuple[str, str]:
        """从 nav 接口拉取 wbi img/sub key"""
        url = "https://api.bilibili.com/x/web-interface/nav"
        data = BilibiliParser._fetch_json(url)
        wbi_img = (((data or {}).get("data") or {}).get("wbi_img")) or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        def _extract_key(u: str) -> str:
            filename = u.rsplit("/", 1)[-1]
            return filename.split(".")[0]
        img_key = _extract_key(img_url)
        sub_key = _extract_key(sub_url)
        return img_key, sub_key

    @classmethod
    def _gen_mixin_key(cls) -> str:
        now = time.time()
        if cls._cached_mixin_key and (now - cls._cached_at) < cls._cache_ttl_seconds:
            return cls._cached_mixin_key
        img_key, sub_key = cls._fetch_wbi_keys()
        raw = (img_key + sub_key)
        if len(raw) < 64:
            cls._logger.warning(f"WBI key length insufficient: {len(raw)}")
            raise ValueError("WBI key length insufficient")
        mixed = ''.join(raw[i] for i in cls._mixin_key_indices)[:32]
        cls._cached_mixin_key = mixed
        cls._cached_at = now
        return mixed

    @classmethod
    def sign_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """生成 wts 和 w_rid 并返回带签名的参数副本"""
        mixin_key = cls._gen_mixin_key()
        # 复制并清洗参数
        safe_params: Dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str):
                v2 = re.sub(r"[!'()*]", "", v)
            else:
                v2 = v
            safe_params[k] = v2
        # 加入 wts
        wts = int(time.time())
        safe_params["wts"] = wts
        # 排序并 urlencode
        items = sorted(safe_params.items(), key=lambda x: x[0])
        query = urllib.parse.urlencode(items, doseq=True)
        w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        safe_params["w_rid"] = w_rid
        return safe_params



class BilibiliAutoSendHandler(BaseEventHandler):
    """收到包含哔哩哔哩视频链接的消息后，自动解析并发送视频。"""

    _logger = get_logger("plugin.bilibili_video_sender.handler")

    event_type = EventType.ON_MESSAGE
    handler_name = "bilibili_auto_send_handler"
    handler_description = "解析B站视频链接并发送视频"
    # 提高 ON_MESSAGE 阶段的执行优先级，尽量早于其他插件处理
    weight = 50
    intercept_message = False

    def set_plugin_config(self, plugin_config: Dict) -> None:
        """设置插件配置并根据配置决定是否拦截消息链路"""
        super().set_plugin_config(plugin_config)
        # 仅在需要阻止 AI 回复时启用拦截，避免不必要的同步阻塞
        self.intercept_message = bool(self.get_config("bilibili.block_ai_reply", False))

    # 说明：
    # 1) MaiBot 文档里仅对 Command 组件明确说明“第三个 bool 可阻止后续处理”；
    #    EventHandler 的阻断条件在文档中并未写清楚。
    # 2) 根据 MaiBot 核心实现（events_manager.py）：只有 intercept_message=True 的
    #    EventHandler 才会同步执行，并使用 execute() 返回值里的 continue_processing
    #    来控制是否继续后续链路；否则为异步执行，continue_processing 会被忽略。
    # 3) 本插件在 block_ai_reply=True 时才启用拦截模式，以便在识别到 B 站链接后
    #    返回 continue_processing=False 来阻止后续 AI 回复；未识别到链接则返回 True 放行。

    def _should_return_5_tuple(self) -> bool:
        """判断是否应该返回5元组（基于events_manager版本）
        
        Returns:
            bool: True表示返回5元组，False表示返回3元组
        """
        # 默认与配置项一致（新版 events_manager）
        return self.get_config("plugin.use_new_events_manager", True)
    
    def _make_return_value(self, success: bool, continue_processing: bool, result: str | None) -> Tuple:
        """根据版本配置生成返回值
        
        Args:
            success: 执行是否成功
            continue_processing: 是否继续处理后续事件
            result: 执行结果描述
            
        Returns:
            Tuple: 根据配置返回3元组或5元组
        """
        if self._should_return_5_tuple():
            # 新版本：返回5元组 (success, continue_processing, result, modified_message, metadata)
            return success, continue_processing, result, None, None
        else:
            # 旧版本：返回3元组 (success, continue_processing, result)
            return success, continue_processing, result

    def _is_private_message(self, message: MaiMessages) -> bool:
        """检测消息是否为私聊消息"""
        
        # 方法1：从message_base_info中获取group_id，如果没有group_id则为私聊
        if message.message_base_info:
            group_id = message.message_base_info.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                self._logger.debug("检测到私聊消息（无group_id）")
                return True
            else:
                self._logger.debug(f"检测到群聊消息（group_id: {group_id}）")
                return False
        
        # 方法2：从additional_data中获取
        if message.additional_data:
            group_id = message.additional_data.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                self._logger.debug("检测到私聊消息（additional_data无group_id）")
                return True
            else:
                self._logger.debug(f"检测到群聊消息（additional_data group_id: {group_id}）")
                return False
        
        # 默认当作群聊处理
        self._logger.debug("无法确定消息类型，默认当作群聊处理")
        return False
    
    def _get_user_id(self, message: MaiMessages) -> str | None:
        """从消息中获取用户ID"""
        # 方法1：从message_base_info中获取
        if message.message_base_info:
            user_id = message.message_base_info.get("user_id")
            if user_id:
                return str(user_id)
        
        # 方法2：从additional_data中获取
        if message.additional_data:
            user_id = message.additional_data.get("user_id")
            if user_id:
                return str(user_id)
        
        return None

    def _get_group_id(self, message: MaiMessages) -> str | None:
        """从消息中获取群ID"""
        # 方法1：从message_base_info中获取
        if message.message_base_info:
            group_id = message.message_base_info.get("group_id")
            if group_id and group_id != "" and group_id != "0":
                return str(group_id)
        
        # 方法2：从additional_data中获取
        if message.additional_data:
            group_id = message.additional_data.get("group_id")
            if group_id and group_id != "" and group_id != "0":
                return str(group_id)
        
        return None

    def _get_stream_id(self, message: MaiMessages) -> str | None:
        """从消息中获取stream_id"""
        
        # 方法1：直接从message对象的stream_id属性获取
        if message.stream_id:
            return message.stream_id
            
        # 方法2：从chat_stream属性获取
        if hasattr(message, 'chat_stream') and message.chat_stream:
            stream_id = getattr(message.chat_stream, 'stream_id', None)
            if stream_id:
                return stream_id
        
        # 方法3：从message_base_info中获取
        if message.message_base_info:
            # 尝试从message_base_info中提取必要信息生成stream_id
            try:
                from src.chat.message_receive.chat_stream import get_chat_manager
                platform = message.message_base_info.get("platform")
                user_id = message.message_base_info.get("user_id")
                group_id = message.message_base_info.get("group_id")
                
                if platform and (user_id or group_id):
                    chat_manager = get_chat_manager()
                    if group_id:
                        stream_id = chat_manager.get_stream_id(platform, group_id, True)
                    else:
                        stream_id = chat_manager.get_stream_id(platform, user_id, False)
                    
                    if stream_id:
                        return stream_id
            except Exception as e:
                self._logger.error(f"方法3失败：{e}")
        
        # 方法4：从additional_data中查找
        if message.additional_data:
            stream_id = message.additional_data.get("stream_id")
            if stream_id:
                return stream_id
        
        # 如果所有方法都失败，返回None
        self._logger.error("无法获取stream_id")
        return None

    async def _send_text(self, content: str, stream_id: str) -> bool:
        """发送文本消息"""
        try:
            store_text = bool(self.get_config("bilibili.store_plugin_text", True))
            return await send_api.text_to_stream(content, stream_id, storage_message=store_text)
        except Exception as e:
            # 记录错误但不抛出异常，避免影响其他处理器
            return False

    async def _send_private_video(self, original_path: str, converted_path: str, user_id: str) -> bool:
        """通过API发送私聊视频
        
        Args:
            original_path: 原始文件路径（用于文件检查）
            converted_path: 转换后的路径（用于发送URI）
            user_id: 目标用户ID
        """
        
        try:
            # 获取配置的端口
            port = self.get_config("api.port", 5700)
            host = self.get_config("api.host", "napcat")
            token = str(self.get_config("api.token", "")).strip()

            api_url = f"http://{host}:{port}/send_private_msg"   # 群聊就用 send_group_msg


            
            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                self._logger.error(f"视频文件不存在: {original_path}")
                return False
            
            # 构造本地文件路径，使用file://协议（使用转换后路径）
            file_uri = converted_path
            if not converted_path.startswith(("http://", "https://", "file://")):
                file_uri = "file:///" + urllib.request.pathname2url(converted_path).lstrip("/")
            
            self._logger.debug(f"Private video send - original path: {original_path}")
            self._logger.debug(f"Private video send - converted path: {converted_path}")
            self._logger.debug(f"Private video send - send URI: {file_uri}")
            
            # 构造请求数据
            request_data = {
                "user_id": user_id,
                "message": [
                    {
                        "type": "video",
                        "data": {
                            "file": file_uri
                        }
                    }
                ]
            }
            
            self._logger.debug(f"Sending private video API request: {api_url}")
            self._logger.debug(f"Request data: {request_data}")
            # 构建请求头（必要时携带 Token）
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            # 发送API请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=request_data, headers=headers, timeout=300) as response:
                    if response.status == 200:
                        result = await response.json()
                        self._logger.debug(f"Private video sent successfully: {result}")
                        return True
                    if response.status in (401, 403) and token:
                        error_text = await response.text()
                        self._logger.warning(
                            f"Private video auth failed ({response.status}), retrying with access_token"
                        )
                        retry_url = f"{api_url}?access_token={urllib.parse.quote(token)}"
                        async with session.post(retry_url, json=request_data, headers=headers, timeout=300) as retry_response:
                            if retry_response.status == 200:
                                result = await retry_response.json()
                                self._logger.debug(f"Private video sent successfully (retry): {result}")
                                return True
                            error_text = await retry_response.text()
                            self._logger.error(
                                f"Failed to send private video (retry): HTTP {retry_response.status}, {error_text}"
                            )
                            return False
                    error_text = await response.text()
                    self._logger.error(f"Failed to send private video: HTTP {response.status}, {error_text}")
                    return False
                        
        except asyncio.TimeoutError:
            self._logger.error("Private video sending timeout")
            return False
        except Exception as e:
            self._logger.error(f"Private video sending error: {e}")
            return False

    async def _send_group_video(self, original_path: str, converted_path: str, group_id: str) -> bool:
        """通过API发送群视频
        
        Args:
            original_path: 原始文件路径（用于文件检查）
            converted_path: 转换后的路径（用于发送URI）
            group_id: 目标群ID
        """
        
        try:
            # 获取配置的端口
            port = self.get_config("api.port", 5700)
            host = self.get_config("api.host", "napcat")
            token = str(self.get_config("api.token", "")).strip()

            api_url = f"http://{host}:{port}/send_group_msg"   # 群聊就用 send_group_msg


            
            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                self._logger.error(f"视频文件不存在: {original_path}")
                return False
            
            # 构造本地文件路径，使用file://协议（使用转换后路径）
            file_uri = converted_path
            if not converted_path.startswith(("http://", "https://", "file://")):
                file_uri = "file:///" + urllib.request.pathname2url(converted_path).lstrip("/")
            
            self._logger.debug(f"Group video send - original path: {original_path}")
            self._logger.debug(f"Group video send - converted path: {converted_path}")
            self._logger.debug(f"Group video send - send URI: {file_uri}")
            
            # 构造请求数据
            request_data = {
                "group_id": group_id,
                "message": [
                    {
                        "type": "video",
                        "data": {
                            "file": file_uri
                        }
                    }
                ]
            }
            
            self._logger.debug(f"Sending group video API request: {api_url}")
            self._logger.debug(f"Request data: {request_data}")
            # 构建请求头（必要时携带 Token）
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            # 发送API请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=request_data, headers=headers, timeout=300) as response:
                    if response.status == 200:
                        result = await response.json()
                        self._logger.debug(f"Group video sent successfully: {result}")
                        return True
                    if response.status in (401, 403) and token:
                        error_text = await response.text()
                        self._logger.warning(
                            f"Group video auth failed ({response.status}), retrying with access_token"
                        )
                        retry_url = f"{api_url}?access_token={urllib.parse.quote(token)}"
                        async with session.post(retry_url, json=request_data, headers=headers, timeout=300) as retry_response:
                            if retry_response.status == 200:
                                result = await retry_response.json()
                                self._logger.debug(f"Group video sent successfully (retry): {result}")
                                return True
                            error_text = await retry_response.text()
                            self._logger.error(
                                f"Failed to send group video (retry): HTTP {retry_response.status}, {error_text}"
                            )
                            return False
                    error_text = await response.text()
                    self._logger.error(f"Failed to send group video: HTTP {response.status}, {error_text}")
                    return False
                        
        except asyncio.TimeoutError:
            self._logger.error("Group video sending timeout")
            return False
        except Exception as e:
            self._logger.error(f"Group video sending error: {e}")
            return False

    async def execute(self, message: MaiMessages) -> Tuple[bool, bool, str | None]:

        if not self.get_config("plugin.enabled", True):
            self._logger.debug("插件已禁用，退出处理")
            return self._make_return_value(True, True, None)

        # raw_message 保留原始 CQ 文本（用于 @ 判定与文本兜底提取）
        raw_message: str = getattr(message, "raw_message", "") or ""
        parse_source = raw_message
        url = ""
        # message_segments 提供结构化消息段（如 miniapp_card / at / mention_bot）
        segments: List[Seg] = list(getattr(message, "message_segments", []) or [])

        # 启用时优先从小程序卡片提取 B 站链接，避免被其他文本噪声干扰
        if self.get_config("parser.enable_miniapp_card", False):
            for seg in segments:
                if getattr(seg, "type", None) != "miniapp_card":
                    continue
                seg_data = getattr(seg, "data", None)
                if isinstance(seg_data, dict):
                    source_url = str(seg_data.get("source_url", "") or "")
                else:
                    source_url = str(getattr(seg_data, "source_url", "") or "")
                if not source_url:
                    continue
                miniapp_url = BilibiliParser.find_first_bilibili_url(source_url)
                if miniapp_url:
                    parse_source = source_url
                    url = miniapp_url
                    break

        # 小程序未命中时，回退到 raw_message 提取普通链接
        if not url:
            url = BilibiliParser.find_first_bilibili_url(raw_message)
            parse_source = raw_message
        if not url:
            return self._make_return_value(True, True, None)

        # 群聊仅当被 @ 时才处理（判断 raw_message 中的 CQ 码）
        if self.get_config("bilibili.group_at_only", False) and not self._is_private_message(message):
            bot_qq = str(getattr(global_config.bot, "qq_account", "") or "").strip()
            if not bot_qq:
                self._logger.warning("group_at_only 已开启，但 bot qq_account 为空，无法判断 @ 目标")
                return self._make_return_value(True, True, None)

            # 先用 raw_message 的 CQ 码判定是否 @ 到机器人
            mentioned = bool(re.search(rf"\[CQ:at,qq={re.escape(bot_qq)}\]", raw_message))
            # 再兼容适配层注入的提及标记
            if not mentioned and isinstance(getattr(message, "additional_data", None), dict):
                additional_data = message.additional_data
                if additional_data.get("at_bot") is True:
                    mentioned = True
                elif additional_data.get("is_mentioned") is True:
                    mentioned = True
            # 最后从结构化消息段兜底判定（mention_bot / at）
            if not mentioned:
                for seg in segments:
                    seg_type = getattr(seg, "type", None)
                    if seg_type == "mention_bot":
                        mentioned = True
                        break
                    if seg_type == "at":
                        seg_data = getattr(seg, "data", None)
                        if isinstance(seg_data, dict):
                            mention_qq = str(seg_data.get("qq", "") or "").strip()
                        else:
                            mention_qq = str(getattr(seg_data, "qq", "") or "").strip()
                        if mention_qq == bot_qq:
                            mentioned = True
                            break

            if not mentioned:
                return self._make_return_value(True, True, None)

        # 优先从实际解析来源提取 qn；小程序场景再回退 raw_message 兜底
        fallback_qn = BilibiliParser._extract_qn_from_text(parse_source)
        if fallback_qn is None and parse_source != raw_message:
            fallback_qn = BilibiliParser._extract_qn_from_text(raw_message)

        self._logger.info("Bilibili video link detected", url=url, qn_from_text=fallback_qn)
        # 检测到视频链接后，是否阻止后续 AI 回复
        block_ai_reply = self.get_config("bilibili.block_ai_reply", False)
        continue_processing = not block_ai_reply

        # 获取stream_id用于发送消息
        stream_id = self._get_stream_id(message)
        if not stream_id:
            self._logger.error("无法获取聊天流ID，尝试备选方案")

            # 备选方案：尝试从message_base_info提取用户信息，直接向用户发送消息
            try:
                from src.chat.message_receive.chat_stream import get_chat_manager

                # 尝试提取平台和用户ID
                platform = None
                user_id = None

                # 从message_base_info中提取
                if message.message_base_info:
                    platform = message.message_base_info.get("platform")
                    user_id = message.message_base_info.get("user_id")

                # 从additional_data中提取
                if not platform and not user_id and message.additional_data:
                    platform = message.additional_data.get("platform")
                    user_id = message.additional_data.get("user_id")

                if platform and user_id:
                    # 创建一个临时的stream_id
                    chat_manager = get_chat_manager()
                    stream_id = chat_manager.get_stream_id(platform, user_id, False)
                else:
                    self._logger.error("备选方案失败：无法获取平台和用户ID")
                    return self._make_return_value(True, continue_processing, "无法获取聊天流ID")
            except Exception as e:
                self._logger.error(f"备选方案失败：{e}")
                return self._make_return_value(True, continue_processing, "无法获取聊天流ID")

        async def _process_video() -> Tuple[bool, bool, str | None]:
            try:
                # （原有的FFmpeg同步检查已移除，改为在后台初始化缓存）

                # 读取并记录配置
                config_opts = {
                    # 硬编码配置项
                    "use_wbi": True,
                    "prefer_dash": True,
                    "fnval": 4048,
                    "fourk": False,
                    "qn": BilibiliParser._safe_int(self.get_config("bilibili.qn", 0)),
                    "qn_strict": self.get_config("bilibili.qn_strict", False),
                    "platform": "pc",
                    "high_quality": False,
                    "try_look": False,
                    # 从配置文件读取的配置项
                    "sessdata": self.get_config("bilibili.sessdata", ""),
                    "buvid3": self.get_config("bilibili.buvid3", ""),
                }

                # 检查鉴权配置
                if not config_opts["sessdata"]:
                    self._logger.debug("No SESSDATA configured, using guest mode")
                    if config_opts["qn"] >= 64 and config_opts["qn"] != 0:
                        self._logger.warning(f"Requested quality {config_opts['qn']} but not logged in, may fail")
                if not config_opts["buvid3"]:
                    self._logger.debug("No Buvid3 configured, session generation may fail")

                # 执行配置验证
                validation_result = BilibiliParser.validate_config(config_opts)
                if not validation_result["valid"]:
                    self._logger.error("配置验证失败，但继续尝试处理")
                if validation_result["warnings"]:
                    for warning in validation_result["warnings"]:
                        self._logger.debug(f"配置警告: {warning}")
                if validation_result["recommendations"]:
                    for rec in validation_result["recommendations"]:
                        self._logger.debug(f"配置建议: {rec}")

                loop = asyncio.get_running_loop()

                def _blocking() -> Optional[Tuple[BilibiliVideoInfo, Dict[str, Any], str]]:
                    # 在后台线程处理短链接跳转，带重试机制
                    target_url = url
                    if "b23.tv" in target_url:
                        # 短链接解析重试：最多3次尝试（1次原始 + 2次重试）
                        max_retries = 3
                        for attempt in range(max_retries):
                            try:
                                self._logger.debug(f"Resolving short link (attempt {attempt + 1}/{max_retries}): {target_url}")
                                target_url = BilibiliParser._follow_redirect(target_url)
                                self._logger.debug(f"Resolved to: {target_url}")
                                break  # 成功则跳出重试循环
                            except Exception as e:
                                self._logger.warning(f"Failed to resolve short link (attempt {attempt + 1}/{max_retries}): {e}")
                                if attempt < max_retries - 1:
                                    # 线性退避：第1次重试等1秒，第2次重试等2秒
                                    wait_time = attempt + 1
                                    self._logger.info(f"Retrying in {wait_time} seconds...")
                                    time.sleep(wait_time)
                                else:
                                    # 所有重试都失败，记录错误并使用原始URL继续
                                    self._logger.error(f"Failed to resolve short link after {max_retries} attempts, using original URL")

                    target_url = BilibiliParser._sanitize_url(target_url)

                    # URL 参数覆盖:在解析跳转后的 URL 中提取 qn 参数
                    url_qn = BilibiliParser._extract_qn_param(target_url)
                    if url_qn is None and fallback_qn is not None:
                        url_qn = fallback_qn
                        self._logger.info("Fallback qn detected from raw text", qn=url_qn)
                    if url_qn is not None:
                        self._logger.info(
                            f"URL quality parameter detected: qn={url_qn}, overriding config value ({config_opts['qn']})"
                        )
                        config_opts["qn"] = url_qn

                    # 同时也在这里检查FFmpeg可用性（第一次检查比较慢，因为要扫描硬件）
                    if not _ffmpeg_manager._cached_check_result:
                        self._logger.debug("Initializing FFmpeg manager cache in background...")
                        _ffmpeg_manager.check_ffmpeg_availability()

                    # 视频信息解析重试：最多3次尝试
                    info = None
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            self._logger.debug(f"Parsing video info (attempt {attempt + 1}/{max_retries}): {target_url}")
                            info = BilibiliParser.get_view_info_by_url(target_url, config_opts)
                            if info:
                                self._logger.debug("Video info parsed", title=info.title, aid=info.aid, cid=info.cid)
                                break  # 成功则跳出重试循环
                            else:
                                self._logger.warning(f"Failed to parse video info (attempt {attempt + 1}/{max_retries}): returned None")
                        except Exception as e:
                            self._logger.warning(f"Failed to parse video info (attempt {attempt + 1}/{max_retries}): {e}")
                        
                        if attempt < max_retries - 1:
                            wait_time = attempt + 1
                            self._logger.info(f"Retrying in {wait_time} seconds...")
                            time.sleep(wait_time)
                    
                    if not info:
                        self._logger.error(f"Failed to parse video info after {max_retries} attempts", url=target_url)
                        return None

                    sources, status = BilibiliParser.get_play_urls(info.aid, info.cid, config_opts)
                    source_type = sources.get("type") if sources else "none"
                    self._logger.debug("Playback sources fetched", status=status, source_type=source_type, title=info.title)

                    return info, sources, status

                try:
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
                            f"最大允许时长为 {max_minutes}分{max_seconds}秒，已通过在线API检测并拒绝下载。"
                        )
                        self._logger.warning(
                            f"Video duration (API) exceeds limit: {info.duration}s > {max_video_duration}s"
                        )
                        await self._send_text(error_msg, stream_id)
                        return self._make_return_value(True, continue_processing, "视频时长超过限制(API)")
                    else:
                        self._logger.debug(f"Early duration check passed (API): {info.duration}s <= {max_video_duration}s")

                # 发送解析成功消息
                success_message = "解析成功"
                selected_qn_name = config_opts.get("selected_qn_name")
                requested_qn_name = config_opts.get("requested_qn_name")
                selected_qn = config_opts.get("selected_qn")
                requested_qn = config_opts.get("requested_qn")
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
                            "Range": "bytes=0-"  # 支持 Range 请求，避免部分 CDN 403
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
                                        progress_bar = ProgressBar(total_size, f"{desc} (候选{idx}/{len(url_list)})", 30)
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

                            self._logger.debug("DASH format assumed", video_urls=len(video_urls), audio_urls=len(audio_urls))

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
                                    audio_format = "none"
                                    if ffprobe_path:
                                        probe_cmd = [
                                            ffprobe_path,
                                            "-v", "error",
                                            "-show_entries", "format=format_name",
                                            "-of", "default=noprint_wrappers=1:nokey=1",
                                            video_temp
                                        ]
                                        try:
                                            video_format = subprocess.run(
                                                probe_cmd, capture_output=True, text=False
                                            ).stdout.decode("utf-8", errors="replace").strip()
                                        except Exception as e:
                                            self._logger.warning(f"Unable to check video format: {str(e)}")
                                        if audio_temp and os.path.exists(audio_temp):
                                            probe_cmd = [
                                                ffprobe_path,
                                                "-v", "error",
                                                "-show_entries", "format=format_name",
                                                "-of", "default=noprint_wrappers=1:nokey=1",
                                                audio_temp
                                            ]
                                            try:
                                                audio_format = subprocess.run(
                                                    probe_cmd, capture_output=True, text=False
                                                ).stdout.decode("utf-8", errors="replace").strip()
                                            except Exception as e:
                                                self._logger.warning(f"Unable to check audio format: {str(e)}")
                                    else:
                                        self._logger.warning(f"ffprobe not found, unable to check file format: {ffprobe_path}")

                                    if "m4s" in video_format.lower() or video_temp.lower().endswith(".m4s"):
                                        if audio_temp and os.path.exists(audio_temp):
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i", video_temp,
                                                "-i", audio_temp,
                                                "-c:v", "copy",  # 复制视频流，不重新编码
                                                "-c:a", "aac",   # 将音频转换为aac格式以确保兼容性
                                                "-strict", "experimental",
                                                "-b:a", "192k",  # 设置音频比特率
                                                "-y", temp_path
                                            ]
                                        else:
                                            ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", temp_path]
                                    else:
                                        if audio_temp and os.path.exists(audio_temp):
                                            ffmpeg_cmd = [
                                                ffmpeg_path,
                                                "-i", video_temp,
                                                "-i", audio_temp,
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-y", temp_path
                                            ]
                                        else:
                                            ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", temp_path]

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
                                        stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
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
                                        fallback_err = fallback_result.stderr.decode("utf-8", errors="replace") if fallback_result.stderr else ""
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
                                        stderr_text = remux_result.stderr.decode("utf-8", errors="replace") if remux_result.stderr else ""
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
                caption = f"{info.title}"

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
                    video_size_mb = await loop.run_in_executor(
                        None, lambda: os.path.getsize(temp_path) / (1024 * 1024)
                    )
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
                                )
                            }
                        }
                        compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"], config_dict)

                        # 执行压缩（这是最耗时的部分）
                        if compressor.compress_video(temp_path, compressed_path, max_video_size_mb, compression_quality):
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
                if (
                    video_size_mb > max_video_size_mb
                    and enable_compression
                    and ffmpeg_info["ffmpeg_available"]
                ):

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
                    # 在发送前进行WSL路径转换
                    enable_conversion = self.get_config("wsl.enable_path_conversion", False)
                    converted_path = convert_windows_to_wsl_path(path) if enable_conversion else path

                    self._logger.debug(f"Sending single video - path conversion enabled: {enable_conversion}")
                    self._logger.debug(f"Sending single video - original path: {path}")
                    self._logger.debug(f"Sending single video - converted path: {converted_path}")

                    # 检查是否为私聊消息
                    is_private = self._is_private_message(message)

                    if is_private:
                        # 私聊消息，使用专用API发送
                        user_id = self._get_user_id(message)
                        if user_id:
                            self._logger.debug(f"Private message detected, sending private video API to user: {user_id}")
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
                except Exception as e:
                    self._logger.warning(f"Failed to delete temporary file: {e}")

                self._logger.info("Bilibili video processing completed")
                return self._make_return_value(True, continue_processing, "已发送视频（若宿主支持）")
            except Exception as e:
                self._logger.error(f"视频处理异常: {e}")
                return self._make_return_value(True, continue_processing, "解析失败")

        # block_ai_reply=True 时立即返回，避免阻塞事件链路；后台任务继续处理下载与发送
        if block_ai_reply:
            asyncio.create_task(_process_video())
            return self._make_return_value(True, False, "已接管B站链接处理")

        return await _process_video()

@register_plugin
class BilibiliVideoSenderPlugin(BasePlugin):
    """B站视频解析与自动发送插件。"""
    
    _logger = get_logger("plugin.bilibili_video_sender.plugin")

    plugin_name: str = "bilibili_video_sender_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "ffmpeg": "FFmpeg相关配置",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.3.6", description="配置版本"),
            "use_new_events_manager": ConfigField(type=bool, default=True, description="是否使用新版 events_manager（0.10.2 及以上版本设为 True，否则设为 False）"),
        },
        "bilibili": {
            "sessdata": ConfigField(type=str, default="", description="B 站登录 Cookie 中的 SESSDATA 值（用于获取高清晰度视频）"),
            "buvid3": ConfigField(type=str, default="", description="B 站设备标识 Buvid3（可选，用于生成 session 参数）"),
            "qn": ConfigField(type=int, default=0, description="清晰度设置(qn)，0 为自动（登录默认 720P，未登录默认 480P）。常见值：16 = 360P, 32 = 480P, 64 = 720P, 74 = 720P60, 80 = 1080P, 112 = 1080P+, 116 = 1080P60, 120 = 4K, 125 = HDR, 126 = 杜比视界, 127 = 8K"),
            "qn_strict": ConfigField(type=bool, default=False, description="是否严格按 qn 选择清晰度。False 时会在可用流中自动降级/回退；True 时不可用则报错"),
            "group_at_only": ConfigField(type=bool, default=False, description="群聊中仅当被 @ 时才处理 B 站链接"),
            "block_ai_reply": ConfigField(type=bool, default=True, description="检测到 B 站视频链接后是否阻止后续 AI 回复（仅影响本次事件链路）（旧版 events_manager 下可能无效）"),
            "store_plugin_text": ConfigField(type=bool, default=False, description="插件发送的文本消息是否写入历史记录（False 则不入库）"),
            "enable_video_compression": ConfigField(type=bool, default=True, description="是否启用视频压缩功能"),
            "max_video_size_mb": ConfigField(type=int, default=100, description="视频文件大小限制（MB），超过此大小将进行压缩"),
            "compression_quality": ConfigField(type=int, default=23, description="视频压缩质量 (1-51，数值越小质量越高，推荐 18-28)"),
            "enable_duration_limit": ConfigField(type=bool, default=True, description="是否启用视频时长限制"),
            "max_video_duration": ConfigField(type=int, default=600, description="视频最大时长限制（秒），超过此时长将拒绝发送"),
        },
        "parser" : {
            "enable_miniapp_card": ConfigField(type=bool, default=False, description="是否允许解析B站小卡片"),
        },
        "ffmpeg": {
            "show_warnings": ConfigField(type=bool, default=True, description="是否显示 FFmpeg 相关警告信息"),
            "enable_hardware_acceleration": ConfigField(type=bool, default=True, description="是否启用硬件加速自动检测（推荐开启，可大幅提升视频压缩速度）"),
            "force_encoder": ConfigField(type=str, default="", description="强制使用特定编码器（留空则自动选择，可选值：libx264 / h264_nvenc / h264_qsv / h264_amf / h264_videotoolbox）"),
            "encoder_priority": ConfigField(type=list, default=["nvidia", "intel", "amd", "apple"], description="编码器优先级（当检测到多个硬件编码器时的选择顺序）"),
        },
        "wsl": {
            "enable_path_conversion": ConfigField(type=bool, default=False, description="是否启用 Windows 到 WSL 的路径转换"),
        },
        "api": {
            "host": ConfigField(
                type=str,
                default="127.0.0.1",
                description="OneBot HTTP API 主机名/地址：Docker 部署时填服务名 napcat；非 Docker 本机部署可填 localhost/127.0.0.1；跨机部署填对方内网 IP 或域名",
            ),
            "port": ConfigField(
                type=int,
                default=5700,
                description="OneBot HTTP API 端口号：需与 NapCat 的 HTTP Server 端口一致，默认 5700",
            ),
            "token": ConfigField(
                type=str,
                default="",
                description="OneBot HTTP API Token：当 NapCat HTTP Server 配置了 Token 时需填写相同值；未启用鉴权可留空",
            ),
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (BilibiliAutoSendHandler.get_handler_info(), BilibiliAutoSendHandler),
        ]
