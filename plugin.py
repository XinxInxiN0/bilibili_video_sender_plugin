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




def convert_windows_to_wsl_path(windows_path: str) -> str:
    """将Windows路径转换为WSL路径
    
    例如：E:\path\to\file.mp4 -> /mnt/e/path/to/file.mp4
    """
    try:
        # 尝试使用wslpath命令转换路径（从Windows调用WSL）
        try:
            # 在Windows上调用wsl wslpath命令
            result = subprocess.run(['wsl', 'wslpath', '-u', windows_path], 
                                   capture_output=True, text=True, check=True)
            wsl_path = result.stdout.strip()
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


class FFmpegManager:
    """跨平台FFmpeg管理器"""

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
        # 延迟导入日志器，避免循环依赖
        from src.common.logger import get_logger
        logger = get_logger("ffmpeg_manager")

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
            logger.warning(f"不支持的操作系统: {self.system}")
            return None

        # 检查插件内置的ffmpeg
        if os.path.exists(executable_path):
            logger.debug(f"找到插件内置{executable_name}: {executable_path}")
            return executable_path

        # 检查系统PATH中的ffmpeg
        system_executable = shutil.which(executable_name)
        if system_executable:
            logger.debug(f"找到系统{executable_name}: {system_executable}")
            return system_executable

        logger.warning(f"未找到{executable_name}可执行文件")
        return None

    def check_ffmpeg_availability(self) -> Dict[str, Any]:
        """检查FFmpeg可用性"""
        from src.common.logger import get_logger
        logger = get_logger("ffmpeg_manager")

        result = {
            "ffmpeg_available": False,
            "ffprobe_available": False,
            "ffmpeg_path": None,
            "ffprobe_path": None,
            "ffmpeg_version": None,
            "system": self.system
        }

        # 检查ffmpeg
        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            result["ffmpeg_available"] = True
            result["ffmpeg_path"] = ffmpeg_path

            try:
                # 获取ffmpeg版本信息
                cmd = [ffmpeg_path, '-version']
                process = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if process.returncode == 0:
                    version_line = process.stdout.split('\n')[0] if process.stdout else ""
                    result["ffmpeg_version"] = version_line
                    logger.info(f"FFmpeg版本: {version_line}")
            except Exception as e:
                logger.warning(f"获取FFmpeg版本失败: {e}")

        # 检查ffprobe
        ffprobe_path = self.get_ffprobe_path()
        if ffprobe_path:
            result["ffprobe_available"] = True
            result["ffprobe_path"] = ffprobe_path

        logger.info(f"FFmpeg可用性检查结果: ffmpeg={result['ffmpeg_available']}, ffprobe={result['ffprobe_available']}")
        return result


# 全局FFmpeg管理器实例
_ffmpeg_manager = FFmpegManager()


def _prepare_split_dir() -> str:
    """清理并准备插件内的 data/split 目录。
    - 每次下载/分块前调用，确保目录存在且为空。
    """
    # 延迟导入日志器，避免循环依赖
    from src.common.logger import get_logger

    logger = get_logger("bilibili_handler")
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    split_dir = os.path.join(plugin_dir, "data", "split")
    try:
        if os.path.exists(split_dir):
            shutil.rmtree(split_dir)
        os.makedirs(split_dir, exist_ok=True)
        logger.debug(f"分块输出目录: {split_dir}（已清理历史文件）")
    except Exception as e:
        logger.warning(f"准备分块目录失败: {e}")
        os.makedirs(split_dir, exist_ok=True)
    return split_dir


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
    
    def __init__(self, aid: int, cid: int, title: str, bvid: Optional[str] = None):
        self.aid = aid
        self.cid = cid
        self.title = title
        self.bvid = bvid


class BilibiliParser:
    """哔哩哔哩链接解析器。"""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    VIDEO_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?bilibili\.com/video/(?P<bv>BV[\w]+|av\d+)",
        re.IGNORECASE,
    )
    B23_SHORT_PATTERN = re.compile(r"https?://b23\.tv/[\w]+", re.IGNORECASE)

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
    def _fetch_json(url: str) -> Dict[str, Any]:
        req = BilibiliParser._build_request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data = resp.read()
        return json.loads(data.decode("utf-8", errors="ignore"))

    @staticmethod
    def _follow_redirect(url: str) -> str:
        req = BilibiliParser._build_request(url)
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
            try:
                return BilibiliParser._follow_redirect(short.group(0))
            except Exception:
                # 回退为原短链
                return short.group(0)

        # 再匹配标准视频链接
        match = BilibiliParser.VIDEO_URL_PATTERN.search(text)
        if match:
            return match.group(0)
        return None

    @staticmethod
    def get_view_info_by_url(url: str) -> Optional[BilibiliVideoInfo]:
        # 优先解析 BV 号
        bvid = BilibiliParser._extract_bvid(url)

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
        payload = BilibiliParser._fetch_json(api)
        if payload.get("code") != 0:
            return None

        data = payload.get("data", {})
        pages = data.get("pages") or []
        if not pages:
            return None

        first_page = pages[0]
        return BilibiliVideoInfo(
            aid=int(data.get("aid")),
            cid=int(first_page.get("cid")),
            title=str(data.get("title", "")),
            bvid=str(data.get("bvid", "")) or None,
        )

    @staticmethod
    def get_play_urls(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], str]:
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        opts = options or {}
        
        # 配置参数
        logger.info("开始获取视频播放地址")
        
        use_wbi = bool(opts.get("use_wbi", True))
        prefer_dash = bool(opts.get("prefer_dash", True))
        fnval = int(opts.get("fnval", 4048 if prefer_dash else 1))
        fourk = 1 if bool(opts.get("fourk", True)) else 0
        qn = int(opts.get("qn", 0))
        platform = str(opts.get("platform", "pc"))
        high_quality = 1 if bool(opts.get("high_quality", False)) else 0
        try_look = 1 if bool(opts.get("try_look", False)) else 0
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        
        # 鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)
        
        if not has_cookie:
            logger.warning("未提供Cookie，将使用游客模式（清晰度限制）")
        
        # 清晰度选择逻辑优化
        if qn == 0:
            if has_cookie:
                qn = 64  # 登录后默认720P
            else:
                qn = 32  # 未登录默认480P
        else:
            # 检查清晰度权限
            qn_info = {
                6: "240P",
                16: "360P", 
                32: "480P",
                64: "720P",
                80: "1080P",
                112: "1080P+",
                116: "1080P60",
                120: "4K",
                125: "HDR",
                126: "杜比视界"
            }
            qn_name = qn_info.get(qn, f"未知({qn})")
            
            # 清晰度权限检查
            if qn >= 64 and not has_cookie:
                logger.warning(f"请求{qn_name}清晰度但未登录，可能失败")
            if qn >= 80 and not has_cookie:
                logger.warning(f"请求{qn_name}清晰度需要大会员账号")
            if qn >= 116 and not has_cookie:
                logger.warning(f"请求{qn_name}高帧率需要大会员账号")
            if qn >= 125 and not has_cookie:
                logger.warning(f"请求{qn_name}需要大会员账号")

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
        
        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        # 构建请求头：可带 Cookie
        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)
            headers["gaia_source"] = sessdata  # 添加 gaia_source
        else:
            logger.info("使用游客模式")

        # 发起请求
        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            logger.error(f"HTTP请求失败: {e}")
            return [], f"网络请求失败: {e}"
            
        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            logger.error(f"JSON解析失败: {e}")
            return [], "响应数据格式错误"
            
        if payload.get("code") != 0:
            error_msg = payload.get("message", "接口返回错误")
            logger.error(f"API返回错误: code={payload.get('code')}, message={error_msg}")
            return [], error_msg

        logger.info("API请求成功，开始解析响应数据")
        data = payload.get("data", {})

        # 处理dash格式
        dash = data.get("dash")
        if not dash:
            logger.warning("未找到dash格式数据")
            # 检查是否有durl格式
            durl = data.get("durl")
            if durl:
                logger.info(f"找到durl格式数据，共{len(durl)}个文件")
                # 处理durl格式
                candidates = []
                for i, item in enumerate(durl):
                    url = item.get("baseUrl") or item.get("base_url")
                    if url:
                        candidates.append(url.replace("http:", "https:"))
                        logger.info(f"添加durl文件{i+1}: {url[:50]}...")
                if candidates:
                    return candidates, "ok (durl格式)"
            return [], "未找到dash数据"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        logger.info(f"找到{len(videos)}个视频流和{len(audios)}个音频流")
        
        # 记录视频流详细信息
        if videos:
            logger.info("=" * 80)
            logger.info("视频流信息:")
            logger.info("-" * 80)
            logger.info(f"{'序号':<4} {'分辨率':<12} {'编码格式':<25} {'比特率':<10} {'帧率':<10}")
            logger.info("-" * 80)
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                logger.info(f"{i+1:<4} {width}x{height:<8} {codec:<25} {bandwidth//1000:<10}kbps {frame_rate:<10}")
            logger.info("-" * 80)
        
        # 记录音频流详细信息
        if audios:
            logger.info("音频流信息:")
            logger.info("-" * 80)
            logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
            logger.info("-" * 80)
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
            logger.info("-" * 80)
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            logger.info(f"找到{len(dolby_audios)}个杜比音频流")
            if dolby_audios:
                logger.info("杜比音频流信息:")
                logger.info("-" * 80)
                logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
                logger.info("-" * 80)
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
                logger.info("-" * 80)
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            logger.info(f"找到{len(flac_audios)}个Flac音频流")
            if flac_audios:
                logger.info("Flac音频流信息:")
                logger.info("-" * 80)
                logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
                logger.info("-" * 80)
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
                logger.info("-" * 80)
        
        # 合并所有音频流
        all_audios = audios + dolby_audios + flac_audios
        
        if not videos:
            logger.warning("未找到视频流")
            return [], "未找到视频流"
            
        if not all_audios:
            logger.warning("未找到音频流")
        
        # 参考原脚本，按照质量排序（降序）
        videos.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        
        candidates = []
        
        # 参考原脚本，选择最高质量的视频流
        if videos:
            best_video = videos[0]
            video_url = best_video.get("baseUrl") or best_video.get("base_url")
            if video_url:
                candidates.append(video_url.replace("http:", "https:"))
                codec = best_video.get("codecs", "unknown")
                bandwidth = best_video.get("bandwidth", 0)
                width = best_video.get("width", 0)
                height = best_video.get("height", 0)
                logger.info(f"选择最佳视频流: {width}x{height}, {codec}, {bandwidth//1000}kbps")
                
        # 参考原脚本，选择最高质量的音频流
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                logger.info(f"选择最佳音频流: {codec}, {bandwidth//1000}kbps")
                
        if candidates:
            logger.info(f"成功获取{len(candidates)}个播放地址")
            return candidates, "ok"
            
        logger.error("未获取到播放地址")
        return [], "未获取到播放地址"
    
    @staticmethod
    def get_play_urls_force_dash(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], str]:
        """强制获取dash格式的视频和音频流"""
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        opts = options or {}
        
        logger.info(f"=== 强制获取DASH格式 ===")
        logger.info(f"视频ID: aid={aid}, cid={cid}")
        logger.info(f"配置参数: {opts}")
        
        use_wbi = bool(opts.get("use_wbi", True))
        fnval = 4048  # 强制使用DASH格式
        fourk = 1 if bool(opts.get("fourk", True)) else 0
        platform = str(opts.get("platform", "pc"))
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()
        
        # 记录鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)
        logger.info(f"强制DASH鉴权状态: 有Cookie={has_cookie}, 有Buvid3={has_buvid3}")
        
        if not has_cookie:
            logger.warning("强制DASH未提供Cookie，可能影响高清晰度获取")

        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fourk": str(fourk),
            "fnver": "0",
            "fnval": str(fnval),
            "platform": platform,
        }
        
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
        
        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)
            headers["gaia_source"] = sessdata  # 添加 gaia_source

        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            logger.error(f"强制DASH HTTP请求失败: {e}")
            return [], f"强制DASH网络请求失败: {e}"
            
        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            logger.error(f"强制DASH JSON解析失败: {e}")
            return [], "强制DASH响应数据格式错误"
            
        if payload.get("code") != 0:
            error_msg = payload.get("message", "接口返回错误")
            logger.error(f"强制DASH API返回错误: code={payload.get('code')}, message={error_msg}")
            return [], error_msg

        logger.info("强制DASH API请求成功，开始解析响应数据")
        data = payload.get("data", {})
        
        # 检查是否仍然返回durl格式
        durl = data.get("durl")
        if durl:
            logger.info(f"强制DASH请求也返回durl格式，说明该视频只有单文件格式，共{len(durl)}个文件")
            # 记录durl文件信息
            for i, item in enumerate(durl):
                url = item.get("baseUrl") or item.get("base_url")
                size = item.get("size", 0)
                logger.info(f"强制DASH durl文件{i+1}: 大小={size//1024//1024}MB, URL={url[:50]}...")
            return [], "该视频只有单文件格式"
        
        dash = data.get("dash")
        if not dash:
            logger.warning("强制DASH请求也未找到dash数据")
            # 检查其他可能的数据结构
            logger.info(f"强制DASH响应数据结构: {list(data.keys())}")
            return [], "未找到dash数据"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        logger.info(f"强制DASH获取到{len(videos)}个视频流和{len(audios)}个音频流")
        
        # 记录视频流详细信息（表格格式）
        if videos:
            logger.info("=" * 80)
            logger.info("强制DASH视频流信息:")
            logger.info("-" * 80)
            logger.info(f"{'序号':<4} {'分辨率':<12} {'编码格式':<25} {'比特率':<10} {'帧率':<10}")
            logger.info("-" * 80)
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                logger.info(f"{i+1:<4} {width}x{height:<8} {codec:<25} {bandwidth//1000:<10}kbps {frame_rate:<10}")
            logger.info("-" * 80)
        
        # 记录音频流详细信息（表格格式）
        if audios:
            logger.info("强制DASH音频流信息:")
            logger.info("-" * 80)
            logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
            logger.info("-" * 80)
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
            logger.info("-" * 80)
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            if dolby_audios:
                logger.info("强制DASH杜比音频流信息:")
                logger.info("-" * 80)
                logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
                logger.info("-" * 80)
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
                logger.info("-" * 80)
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            if flac_audios:
                logger.info("强制DASH Flac音频流信息:")
                logger.info("-" * 80)
                logger.info(f"{'序号':<4} {'编码格式':<25} {'比特率':<10}")
                logger.info("-" * 80)
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    logger.info(f"{i+1:<4} {codec:<25} {bandwidth//1000:<10}kbps")
                logger.info("-" * 80)
        
        all_audios = audios + dolby_audios + flac_audios
        
        if not videos or not all_audios:
            logger.warning(f"强制DASH请求中缺少视频或音频流: 视频={len(videos)}, 音频={len(all_audios)}")
            return [], "缺少视频或音频流"
        
        # 按照质量排序
        videos.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        
        candidates = []
        
        # 获取最高质量的视频和音频流
        if videos:
            best_video = videos[0]
            video_url = best_video.get("baseUrl") or best_video.get("base_url")
            if video_url:
                candidates.append(video_url.replace("http:", "https:"))
                codec = best_video.get("codecs", "unknown")
                bandwidth = best_video.get("bandwidth", 0)
                width = best_video.get("width", 0)
                height = best_video.get("height", 0)
                logger.info(f"强制DASH选择最佳视频流: {width}x{height}, {codec}, {bandwidth//1000}kbps")
            
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                logger.info(f"强制DASH选择最佳音频流: {codec}, {bandwidth//1000}kbps")
        
        if len(candidates) >= 2:
            logger.info("强制DASH成功获取完整的视频和音频流")
            return candidates, "ok"
        else:
            logger.warning("强制DASH未获取到完整的视频和音频流")
            return candidates, "未获取到完整的视频和音频流"

    @staticmethod
    def validate_config(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """验证配置参数的有效性"""
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
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
            validation_result["warnings"].append("未配置Buvid3，session参数生成可能失败")
            validation_result["recommendations"].append("建议配置Buvid3以确保session参数正常生成")
        else:
            if len(buvid3) < 10:
                validation_result["errors"].append("Buvid3长度异常，可能配置错误")
                validation_result["valid"] = False

        
        # 检查清晰度配置
        qn = int(opts.get("qn", 0))
        if qn > 0:
            qn_info = {
                6: "240P", 16: "360P", 32: "480P", 64: "720P", 80: "1080P",
                112: "1080P+", 116: "1080P60", 120: "4K", 125: "HDR", 126: "杜比视界"
            }
            qn_name = qn_info.get(qn, f"未知({qn})")
            
            if qn >= 64 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}清晰度但未配置Cookie，可能失败")
            if qn >= 80 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}清晰度需要大会员账号")
            if qn >= 116 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}高帧率需要大会员账号")
            if qn >= 125 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}需要大会员账号")
                
            logger.info(f"清晰度配置: {qn_name} (qn={qn})")
        
        # 检查其他配置
        fnval = int(opts.get("fnval", 4048))
        if fnval not in [1, 16, 80, 64, 32, 128, 256, 512, 1024, 2048, 4096, 8192]:
            validation_result["warnings"].append(f"fnval值{fnval}不是标准值，可能影响播放")
            
        platform = str(opts.get("platform", "pc"))
        if platform not in ["pc", "html5"]:
            validation_result["warnings"].append(f"platform值{platform}不是标准值")
            
        # 记录验证结果
        if validation_result["warnings"]:
            logger.warning(f"配置验证警告: {validation_result['warnings']}")
        if validation_result["errors"]:
            logger.error(f"配置验证错误: {validation_result['errors']}")
        if validation_result["recommendations"]:
            logger.info(f"配置建议: {validation_result['recommendations']}")
            
        logger.info(f"配置验证: {'通过' if validation_result['valid'] else '失败'}")
        return validation_result

    @staticmethod
    def get_video_duration(video_path: str) -> Optional[float]:
        """获取视频时长（秒）"""
        try:
            import subprocess

            # 使用跨平台FFmpeg管理器获取ffprobe路径
            ffprobe_path = _ffmpeg_manager.get_ffprobe_path()

            if not ffprobe_path:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                logger.warning("未找到ffprobe，无法获取视频时长")
                return None

            # 使用ffprobe获取视频时长
            cmd = [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.info(f"执行ffprobe命令获取视频时长: {' '.join(cmd)}")

            # 使用正确的编码设置来避免跨平台编码问题
            result = subprocess.run(cmd, capture_output=True, text=False)

            logger.info(f"ffprobe返回码: {result.returncode}")
            if result.stdout:
                stdout_text = result.stdout.decode('utf-8', errors='replace').strip()
                logger.info(f"ffprobe输出: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.decode('utf-8', errors='replace').strip()
                logger.info(f"ffprobe错误: {stderr_text}")

            if result.returncode == 0:
                duration_str = result.stdout.decode('utf-8', errors='replace').strip()
                try:
                    duration = float(duration_str)
                    logger.info(f"成功获取视频时长: {duration}秒")
                    return duration
                except ValueError:
                    logger.warning(f"无法解析视频时长字符串: '{duration_str}'")
                    return None
            else:
                logger.warning(f"ffprobe命令执行失败，返回码: {result.returncode}")
                return None
        except Exception as e:
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.error(f"获取视频时长时发生异常: {e}")
            return None


class VideoCompressor:
    """视频压缩处理类"""
    
    def __init__(self, ffmpeg_path: Optional[str] = None):
        self.ffmpeg_path = ffmpeg_path or _ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            from src.common.logger import get_logger
            logger = get_logger("video_compressor")
            logger.warning("未找到ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = 'ffmpeg'
    
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
            
            from src.common.logger import get_logger
            logger = get_logger("video_compressor")
            
            # 检查输入文件
            if not os.path.exists(input_path):
                logger.error(f"输入文件不存在: {input_path}")
                return False
            
            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            logger.info(f"开始压缩视频: {input_path} ({input_size_mb:.2f}MB) -> 目标大小: {target_size_mb}MB")
            
            # 如果文件已经小于目标大小，直接复制
            if input_size_mb <= target_size_mb:
                import shutil
                shutil.copy2(input_path, output_path)
                logger.info(f"文件大小已符合要求，直接复制: {input_size_mb:.2f}MB")
                return True
            
            # 构建FFmpeg压缩命令
            cmd = [
                self.ffmpeg_path,
                '-i', input_path,
                '-c:v', 'libx264',          # 使用H.264编码器
                '-crf', str(quality),       # 恒定质量模式
                '-preset', 'medium',        # 编码预设（速度vs压缩率平衡）
                '-c:a', 'aac',              # 音频编码器
                '-b:a', '128k',             # 音频比特率
                '-movflags', '+faststart',   # 优化流媒体播放
                '-y',                       # 覆盖输出文件
                output_path
            ]
            
            logger.debug(f"执行FFmpeg压缩命令: {' '.join(cmd)}")
            
            # 执行压缩
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)  # 30分钟超时
            
            if result.returncode == 0:
                # 检查压缩后的文件大小
                if os.path.exists(output_path):
                    output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    logger.info(f"视频压缩成功: {input_size_mb:.2f}MB -> {output_size_mb:.2f}MB (压缩率: {(1-output_size_mb/input_size_mb)*100:.1f}%)")
                    
                    # 如果压缩后仍然过大，尝试更高的压缩率
                    if output_size_mb > target_size_mb and quality < 35:
                        logger.warning(f"压缩后文件仍然过大({output_size_mb:.2f}MB)，尝试更高压缩率")
                        return self.compress_video(input_path, output_path, target_size_mb, quality + 5)
                    
                    return True
                else:
                    logger.error("压缩后文件不存在")
                    return False
            else:
                logger.error(f"视频压缩失败，返回码: {result.returncode}")
                if result.stderr:
                    logger.error(f"FFmpeg错误信息: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.error("视频压缩超时")
            return False
        except Exception as e:
            logger.error(f"视频压缩异常: {e}")
            return False


class VideoSplitter:
    """视频分块处理类"""

    def __init__(self, ffmpeg_path: Optional[str] = None):
        self.ffmpeg_path = ffmpeg_path or _ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            from src.common.logger import get_logger
            logger = get_logger("video_splitter")
            logger.warning("未找到ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = 'ffmpeg'
        
    def split_video_by_size(self, input_path: str, output_dir: str, max_size_mb: int = 100) -> List[str]:
        """
        根据文件大小智能分割视频
        
        Args:
            input_path: 输入视频路径
            output_dir: 输出目录
            max_size_mb: 每个分片的最大大小（MB）
            
        Returns:
            分割后的视频文件路径列表
        """
        try:
            import subprocess
            import os
            
            from src.common.logger import get_logger
            logger = get_logger("video_splitter")
            
            # 获取视频信息
            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            logger.info(f"开始智能分割视频: {input_path} ({input_size_mb:.2f}MB) -> 目标大小: {max_size_mb}MB")
            
            # 获取视频时长
            duration = BilibiliParser.get_video_duration(input_path)
            if not duration:
                logger.error("无法获取视频时长，回退到固定时间分割")
                return self.split_video(input_path, output_dir)
            
            # 计算需要分割的段数
            segments_needed = max(2, int(input_size_mb / max_size_mb) + 1)
            segment_duration = duration / segments_needed
            
            logger.info(f"视频时长: {duration}秒, 预计分割为{segments_needed}段, 每段约{segment_duration:.1f}秒")
            
            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)
            
            # 构建输出文件模式
            output_pattern = os.path.join(output_dir, f"part_%03d.mp4")
            
            # 构建FFmpeg命令 - 使用计算出的分段时间
            cmd = [
                self.ffmpeg_path,
                '-i', input_path,
                '-c', 'copy',  # 复制流，不重新编码
                '-f', 'segment',
                '-segment_time', str(int(segment_duration)),  # 使用计算出的分段时间
                '-reset_timestamps', '1',  # 重置时间戳
                '-segment_start_number', '0',  # 从0开始编号
                '-y',  # 覆盖现有文件
                output_pattern
            ]
            
            logger.debug(f"执行FFmpeg智能分割命令: {' '.join(cmd)}")
            
            # 执行分割
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            
            if result.returncode == 0:
                # 查找生成的分片文件
                split_files = []
                i = 0
                while True:
                    part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                    if os.path.exists(part_path):
                        file_size = os.path.getsize(part_path)
                        if file_size > 0:
                            split_files.append(part_path)
                            file_size_mb = file_size / (1024 * 1024)
                            logger.debug(f"找到分片文件: {part_path}, 大小: {file_size_mb:.2f}MB")
                        else:
                            logger.warning(f"分片文件大小为0，跳过: {part_path}")
                        i += 1
                    else:
                        break
                
                if split_files:
                    logger.info(f"智能分割成功，生成{len(split_files)}个分片")
                    return split_files
                else:
                    logger.error("智能分割完成但未找到分片文件")
                    return []
            else:
                logger.error(f"智能分割失败，返回码: {result.returncode}")
                if result.stderr:
                    logger.error(f"FFmpeg错误: {result.stderr}")
                return []
                
        except Exception as e:
            logger.error(f"智能分割异常: {e}")
            return []
    
    def split_video(self, input_path: str, output_dir: str) -> List[str]:
        """
        将视频分割成3分钟长度的片段（固定时间分割）
        
        Args:
            input_path: 输入视频路径
            output_dir: 输出目录
            
        Returns:
            分割后的视频文件路径列表
        """
        try:
            import subprocess
            import os
            
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            
            logger.debug(f"开始视频分割: 输入={input_path}, 输出目录={output_dir}, 分割间隔=3分钟")
            logger.debug(f"FFmpeg路径: {self.ffmpeg_path}")
            logger.debug(f"输入文件是否存在: {os.path.exists(input_path)}")
            if os.path.exists(input_path):
                input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
                logger.debug(f"输入文件大小: {input_size_mb:.2f}MB")
            
            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)
            
            # 获取输入文件名（不含扩展名），处理中文文件名
            base_name = os.path.splitext(os.path.basename(input_path))[0]
            
            # 为了避免Windows上的中文路径问题，使用英文标识符
            # 构建输出文件模式，使用英文标识符避免编码问题
            output_pattern = os.path.join(output_dir, f"part_%03d.mp4")
            
            # 每3分钟分割一次（180秒）
            
            # 构建FFmpeg命令 - 使用基于时间的分片（每3分钟）
            cmd = [
                self.ffmpeg_path,
                '-i', input_path,
                '-c', 'copy',  # 复制流，不重新编码
                '-f', 'segment',
                '-segment_time', '180',  # 每3分钟分割一次（180秒）
                '-reset_timestamps', '1',  # 重置时间戳
                '-segment_start_number', '0',  # 从0开始编号
                '-avoid_negative_ts', 'make_zero',  # 避免负时间戳
                '-y',  # 覆盖输出文件
                output_pattern
            ]
            
            # 执行分割命令
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.debug(f"执行FFmpeg分割命令: {' '.join(cmd)}")
            
            # 使用正确的编码设置来避免Windows上的编码问题
            # 添加环境变量设置，确保FFmpeg能正常工作
            env = os.environ.copy()
            env['FFREPORT'] = 'file=ffmpeg_debug.log:level=32'  # 启用FFmpeg调试日志
            
            result = subprocess.run(cmd, capture_output=True, text=False, env=env)
            
            logger.debug(f"FFmpeg分割返回码: {result.returncode}")
            if result.stdout:
                stdout_text = result.stdout.decode('utf-8', errors='replace').strip()
                # 成功时标准输出通常为进度/信息
                logger.debug(f"FFmpeg分割stdout: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.decode('utf-8', errors='replace').strip()
                # 注意：FFmpeg 常把普通信息写入 stderr。仅在失败(returncode!=0)时按错误记录
                if result.returncode == 0:
                    logger.debug(f"FFmpeg分割stderr: {stderr_text}")
                else:
                    logger.error(f"FFmpeg分割错误: {stderr_text}")
            
            if result.returncode != 0:
                stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
                logger.error(f"视频分割失败: {stderr_text}")
                # 尝试使用备用分片方法
                logger.info("尝试使用备用分片方法...")
                return self._fallback_split_video(input_path, output_dir)
            
            # 查找生成的分割文件，使用英文标识符
            split_files = []
            i = 0
            max_attempts = 100  # 防止无限循环
            
            # 等待一下，确保文件系统同步
            import time
            time.sleep(1)
            
            while i < max_attempts:
                part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                if os.path.exists(part_path):
                    file_size = os.path.getsize(part_path)
                    if file_size > 0:  # 确保文件不是空的
                        split_files.append(part_path)
                        logger.debug(f"找到分块文件: {part_path}, 大小: {file_size} 字节")
                        i += 1
                    else:
                        logger.warning(f"分块文件大小为0，跳过: {part_path}")
                        break
                else:
                    # 正常结束：下一个顺序分块不存在，停止扫描
                    logger.debug(f"无更多分块，停止扫描。下一个期望: {part_path}")
                    break
            
            if not split_files:
                logger.warning("未找到任何分片文件")
            
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.info(f"视频分割完成，共生成{len(split_files)}个片段")
            
            return split_files
            
        except Exception as e:
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.error(f"视频分割过程中发生错误: {e}")
            return []
    
    def _fallback_split_video(self, input_path: str, output_dir: str) -> List[str]:
        """备用视频分片方法，使用更简单的FFmpeg命令"""
        try:
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            
            logger.info("使用备用分片方法...")
            
            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)
            
            # 使用更简单的分片命令（每3分钟）
            output_pattern = os.path.join(output_dir, "part_%03d.mp4")
            
            cmd = [
                self.ffmpeg_path,
                '-i', input_path,
                '-c', 'copy',
                '-f', 'segment',
                '-segment_time', '180',  # 每3分钟分割一次（180秒）
                '-reset_timestamps', '1',
                '-y',
                output_pattern
            ]
            
            logger.debug(f"备用分片命令: {' '.join(cmd)}")
            
            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=False)
            
            if result.returncode != 0:
                stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
                logger.error(f"备用分片也失败: {stderr_text}")
                return []
            
            # 查找生成的文件
            split_files = []
            i = 0
            while i < 100:  # 最多查找100个文件
                part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                    split_files.append(part_path)
                    logger.debug(f"备用方法找到分片: {part_path}")
                    i += 1
                else:
                    break
            
            return split_files
            
        except Exception as e:
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.error(f"备用分片方法也失败: {e}")
            return []


class BilibiliWbiSigner:
    """WBI 签名工具：自动获取 wbi key 并缓存，生成 w_rid/wts"""

    _mixin_key_indices: List[int] = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 40, 17, 16, 7, 24, 55, 54, 4, 52, 30, 26, 22, 44, 0,
        1, 34, 25, 6, 51, 11, 36, 20, 21,
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

    event_type = EventType.ON_MESSAGE
    handler_name = "bilibili_auto_send_handler"
    handler_description = "解析B站视频链接并发送视频"

    def _is_private_message(self, message: MaiMessages) -> bool:
        """检测消息是否为私聊消息"""
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
        # 方法1：从message_base_info中获取group_id，如果没有group_id则为私聊
        if message.message_base_info:
            group_id = message.message_base_info.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                logger.debug("检测到私聊消息（无group_id）")
                return True
            else:
                logger.debug(f"检测到群聊消息（group_id: {group_id}）")
                return False
        
        # 方法2：从additional_data中获取
        if message.additional_data:
            group_id = message.additional_data.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                logger.debug("检测到私聊消息（additional_data无group_id）")
                return True
            else:
                logger.debug(f"检测到群聊消息（additional_data group_id: {group_id}）")
                return False
        
        # 默认当作群聊处理
        logger.debug("无法确定消息类型，默认当作群聊处理")
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

    def _get_stream_id(self, message: MaiMessages) -> str | None:
        """从消息中获取stream_id"""
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
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
                logger.error(f"方法3失败：{e}")
        
        # 方法4：从additional_data中查找
        if message.additional_data:
            stream_id = message.additional_data.get("stream_id")
            if stream_id:
                return stream_id
        
        # 如果所有方法都失败，返回None
        logger.error("无法获取stream_id")
        return None

    async def _send_text(self, content: str, stream_id: str) -> bool:
        """发送文本消息"""
        try:
            return await send_api.text_to_stream(content, stream_id)
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
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
        try:
            # 获取配置的端口
            port = self.get_config("api.port", 5700)
            api_url = f"http://localhost:{port}/send_private_msg"
            
            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                logger.error(f"视频文件不存在: {original_path}")
                return False
            
            # 构造本地文件路径，使用file://协议（使用转换后路径）
            file_uri = f"file://{converted_path}"
            
            logger.debug(f"私聊视频发送 - 原始路径: {original_path}")
            logger.debug(f"私聊视频发送 - 转换后路径: {converted_path}")
            logger.debug(f"私聊视频发送 - 发送URI: {file_uri}")
            
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
            
            logger.info(f"发送私聊视频API请求: {api_url}")
            logger.info(f"请求数据: {request_data}")
            
            # 发送API请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=request_data, timeout=30) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.info(f"私聊视频发送成功: {result}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"私聊视频发送失败: HTTP {response.status}, {error_text}")
                        return False
                        
        except asyncio.TimeoutError:
            logger.error("私聊视频发送超时")
            return False
        except Exception as e:
            logger.error(f"私聊视频发送异常: {e}")
            return False

    async def execute(self, message: MaiMessages) -> Tuple[bool, bool, str | None]:
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
        logger.info("开始处理B站视频链接")
        
        if not self.get_config("plugin.enabled", True):
            logger.info("插件已禁用，退出处理")
            return True, True, None

        raw: str = getattr(message, "raw_message", "") or ""
        
        url = BilibiliParser.find_first_bilibili_url(raw)
        if not url:
            logger.info("未找到B站链接，退出处理")
            return True, True, None
        
        logger.info(f"找到B站链接: {url}")

        # 获取stream_id用于发送消息
        stream_id = self._get_stream_id(message)
        if not stream_id:
            logger.error("无法获取聊天流ID，尝试备选方案")
            
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
                    logger.error("备选方案失败：无法获取平台和用户ID")
                    return True, True, "无法获取聊天流ID"
            except Exception as e:
                logger.error(f"备选方案失败：{e}")
                return True, True, "无法获取聊天流ID"
        


        # 检查FFmpeg可用性
        ffmpeg_info = _ffmpeg_manager.check_ffmpeg_availability()
        show_ffmpeg_warnings = self.get_config("ffmpeg.show_warnings", True)

        if not ffmpeg_info["ffmpeg_available"]:
            if show_ffmpeg_warnings:
                logger.warning("FFmpeg不可用，视频合并和分块功能将被禁用")
            else:
                logger.debug("FFmpeg不可用，视频合并和分块功能将被禁用")
        if not ffmpeg_info["ffprobe_available"]:
            if show_ffmpeg_warnings:
                logger.warning("ffprobe不可用，视频时长检测将被禁用")
            else:
                logger.debug("ffprobe不可用，视频时长检测将被禁用")

        # 读取并记录配置
        config_opts = {
            "use_wbi": self.get_config("bilibili.use_wbi", True),
            "prefer_dash": self.get_config("bilibili.prefer_dash", True),
            "fnval": self.get_config("bilibili.fnval", 4048),
            "fourk": self.get_config("bilibili.fourk", True),
            "qn": self.get_config("bilibili.qn", 0),
            "platform": self.get_config("bilibili.platform", "pc"),
            "high_quality": self.get_config("bilibili.high_quality", False),
            "try_look": self.get_config("bilibili.try_look", False),
            "sessdata": self.get_config("bilibili.sessdata", ""),
            "buvid3": self.get_config("bilibili.buvid3", ""),
            "enable_video_splitting": self.get_config("bilibili.enable_video_splitting", True),
            "delete_original_after_split": self.get_config("bilibili.delete_original_after_split", True),
        }
        
        # 检查鉴权配置
        if not config_opts['sessdata']:
            logger.warning("未配置SESSDATA，将使用游客模式")
            if config_opts['qn'] >= 64:
                logger.warning(f"请求清晰度{config_opts['qn']}但未登录，可能失败")
        if not config_opts['buvid3']:
            logger.warning("未配置Buvid3，session参数生成可能失败")
            
        # 执行配置验证
        validation_result = BilibiliParser.validate_config(config_opts)
        if not validation_result["valid"]:
            logger.error("配置验证失败，但继续尝试处理")
        if validation_result["warnings"]:
            for warning in validation_result["warnings"]:
                logger.warning(f"配置警告: {warning}")
        if validation_result["recommendations"]:
            for rec in validation_result["recommendations"]:
                logger.info(f"配置建议: {rec}")

        loop = asyncio.get_running_loop()

        def _blocking() -> Optional[Tuple[BilibiliVideoInfo, List[str], str]]:
            info = BilibiliParser.get_view_info_by_url(url)
            if not info:
                logger.error("无法解析视频信息")
                return None
                
            logger.info(f"视频信息解析成功: {info.title}")
            
            urls, status = BilibiliParser.get_play_urls(info.aid, info.cid, config_opts)
            logger.info(f"播放地址获取结果: 状态={status}, URL数量={len(urls)}")
                    
            return info, urls, status

        try:
            result = await loop.run_in_executor(None, _blocking)
        except Exception as exc:  # noqa: BLE001 - 简要兜底
            error_msg = f"解析失败：{exc}"
            logger.error(error_msg)
            await self._send_text(error_msg, stream_id)
            return True, True, "解析失败"

        if not result:
            error_msg = "未能解析该视频链接，请稍后重试。"
            logger.error(error_msg)
            await self._send_text(error_msg, stream_id)
            return True, True, "解析失败"

        info, urls, status = result
        if not urls:
            error_msg = f"解析失败：{status}"
            logger.error(error_msg)
            await self._send_text(error_msg, stream_id)
            return True, True, "解析失败"

        logger.info(f"解析成功: {info.title}")

        # 发送解析成功消息
        await self._send_text("解析成功", stream_id)

        # 下载前清理/准备分块目录
        _prepare_split_dir()

        # 同时发送视频文件
        logger.info("开始下载视频文件...")
        def _download_to_temp(urls: List[str]) -> Optional[str]:
            try:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                
                safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", info.title).strip() or "bilibili_video"
                tmp_dir = tempfile.gettempdir()
                temp_path = os.path.join(tmp_dir, f"{safe_title}.mp4")
                
                logger.info(f"临时文件路径: {temp_path}")
                logger.info(f"临时目录: {tmp_dir}")
                
                # 添加特定的请求头来解决403问题
                # 请求头（含可选 Cookie）
                headers = {
                    "User-Agent": BilibiliParser.USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Range": "bytes=0-"  # 支持断点续传
                }
                sessdata_hdr = self.get_config("bilibili.sessdata", "").strip()
                buvid3_hdr = self.get_config("bilibili.buvid3", "").strip()
                if sessdata_hdr:
                    cookie_parts = [f"SESSDATA={sessdata_hdr}"]
                    if buvid3_hdr:
                        cookie_parts.append(f"buvid3={buvid3_hdr}")
                    headers["Cookie"] = "; ".join(cookie_parts)
                    headers["gaia_source"] = sessdata_hdr  # 添加 gaia_source
                    logger.info("下载时已添加Cookie认证")
                else:
                    logger.warning("下载时未添加Cookie，可能遇到403错误")
                
                # 判断是否是分离的视频和音频流
                # 注意：这里使用外层的urls变量，需要确保在正确的作用域中调用
                if len(urls) >= 2 and (".m4s" in urls[0].lower() or ".m4s" in urls[1].lower()):
                    logger.info("检测到分离的视频和音频流，开始下载并合并")
                    
                    # 下载视频流
                    video_temp = os.path.join(tmp_dir, f"{safe_title}_video.m4s")

                    req = BilibiliParser._build_request(urls[0], headers)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        # 获取文件总大小（如果可用）
                        total_size = resp.headers.get('content-length')
                        total_size = int(total_size) if total_size else 0
                        
                        # 创建进度条
                        progress_bar = ProgressBar(total_size, "视频流下载进度", 30)
                        
                        with open(video_temp, "wb") as f:
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
                        logger.info(f"视频流下载完成，大小: {os.path.getsize(video_temp) // (1024 * 1024)}MB")
                    
                    # 下载音频流
                    audio_temp = os.path.join(tmp_dir, f"{safe_title}_audio.m4s")
                    
                    # 如果有音频URL，下载音频流
                    if len(urls) >= 2:

                        req = BilibiliParser._build_request(urls[1], headers)
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            # 获取文件总大小（如果可用）
                            total_size = resp.headers.get('content-length')
                            total_size = int(total_size) if total_size else 0
                            
                            # 创建进度条
                            progress_bar = ProgressBar(total_size, "音频流下载进度", 30)
                            
                            with open(audio_temp, "wb") as f:
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
                            logger.info(f"音频流下载完成，大小: {os.path.getsize(audio_temp) // (1024 * 1024)}MB")
                    else:
                        logger.warning("没有音频流URL可用")
                        audio_temp = None
                    
                    # 尝试使用FFmpeg合并
                    try:
                        import subprocess
                        import shutil

                        # 使用跨平台FFmpeg管理器获取ffmpeg路径
                        ffmpeg_path = _ffmpeg_manager.get_ffmpeg_path()
                        if ffmpeg_path:
                            logger.info(f"使用FFmpeg路径: {ffmpeg_path}")
                            # 首先检查视频文件格式
                            logger.info("检查文件格式...")
                            
                            # 检查视频文件 - 使用跨平台ffprobe
                            ffprobe_path = _ffmpeg_manager.get_ffprobe_path()
                            if ffprobe_path:
                                probe_cmd = [ffprobe_path, '-v', 'error', '-show_entries', 'format=format_name', '-of', 'default=noprint_wrappers=1:nokey=1', video_temp]
                                try:
                                    video_format = subprocess.run(probe_cmd, capture_output=True, text=False).stdout.decode('utf-8', errors='replace').strip()
                                except Exception as e:
                                    logger.warning(f"无法检查视频格式: {str(e)}")
                                    video_format = "unknown"
                                    
                                # 如果有音频文件，检查其格式
                                audio_format = "none"
                                if audio_temp and os.path.exists(audio_temp):
                                    probe_cmd = [ffprobe_path, '-v', 'error', '-show_entries', 'format=format_name', '-of', 'default=noprint_wrappers=1:nokey=1', audio_temp]
                                    try:
                                        audio_format = subprocess.run(probe_cmd, capture_output=True, text=False).stdout.decode('utf-8', errors='replace').strip()
                                    except Exception as e:
                                        logger.warning(f"无法检查音频格式: {str(e)}")
                            else:
                                logger.warning(f"未找到ffprobe，无法检查文件格式: {ffprobe_path}")
                                video_format = "unknown"
                                audio_format = "none"
                            
                            # 根据文件格式决定处理方式
                            if 'm4s' in video_format.lower() or video_temp.lower().endswith('.m4s'):
                                # 对于m4s格式，需要添加特殊参数
                                if audio_temp and os.path.exists(audio_temp):
                                    ffmpeg_cmd = [
                                        ffmpeg_path, 
                                        '-i', video_temp, 
                                        '-i', audio_temp, 
                                        '-c:v', 'copy',  # 复制视频流，不重新编码
                                        '-c:a', 'aac',   # 将音频转换为aac格式以确保兼容性
                                        '-strict', 'experimental',
                                        '-b:a', '192k',  # 设置音频比特率
                                        '-y', temp_path
                                    ]
                                else:
                                    # 如果没有音频文件，只处理视频
                                    ffmpeg_cmd = [
                                        ffmpeg_path, 
                                        '-i', video_temp, 
                                        '-c:v', 'copy',
                                        '-y', temp_path
                                    ]
                            else:
                                # 标准处理方式
                                if audio_temp and os.path.exists(audio_temp):
                                    ffmpeg_cmd = [
                                        ffmpeg_path, 
                                        '-i', video_temp, 
                                        '-i', audio_temp, 
                                        '-c:v', 'copy', 
                                        '-c:a', 'copy', 
                                        '-y', temp_path
                                    ]
                                else:
                                    # 如果没有音频文件，只处理视频
                                    ffmpeg_cmd = [
                                        ffmpeg_path, 
                                        '-i', video_temp, 
                                        '-c:v', 'copy',
                                        '-y', temp_path
                                    ]
                            
                            logger.info("开始合并视频和音频...")
                            
                            # 使用正确的编码设置来避免Windows上的编码问题
                            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=False)
                            
                            if result.returncode == 0:
                                logger.info("视频和音频合并成功")
                                # 删除临时文件
                                try:
                                    if os.path.exists(video_temp):
                                        os.remove(video_temp)
                                    if audio_temp and os.path.exists(audio_temp):
                                        os.remove(audio_temp)
                                    logger.info("临时文件清理完成")
                                except Exception as e:
                                    logger.warning(f"删除临时文件失败: {str(e)}")
                                    
                                return temp_path
                            else:
                                stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
                                logger.warning(f"FFmpeg合并失败: {stderr_text}")
                        else:
                            logger.warning("未找到FFmpeg，无法合并视频和音频")
                            logger.info("将仅使用视频流文件")
                    except Exception as e:
                        logger.warning(f"合并失败: {str(e)}")
                    
                    # 如果所有方法都失败，返回视频流文件
                    logger.info("将仅使用视频流文件")
                    return video_temp
                
                # 非分离流：仅支持DASH，跳过单文件下载
                logger.info("仅支持DASH分离流，跳过单文件下载")
                return None
            except Exception as e:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                logger.error(f"下载视频失败: {e}")
                return None

        temp_path = await asyncio.get_running_loop().run_in_executor(None, lambda: _download_to_temp(urls))
        if not temp_path:
            logger.warning("视频下载失败")
            return True, True, "视频下载失败"

        logger.info(f"视频下载完成: {temp_path}")
        caption = f"{info.title}"

        # 检查视频时长，决定是否需要分块
        video_duration = BilibiliParser.get_video_duration(temp_path)
        logger.debug(f"检测到视频时长: {video_duration}秒")
        
        # 检查视频文件大小和时长，决定处理策略
        video_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
        logger.debug(f"检测到视频大小: {video_size_mb:.2f}MB")
        
        # 从配置读取相关设置
        enable_splitting = self.get_config("bilibili.enable_video_splitting", True)
        delete_original = self.get_config("bilibili.delete_original_after_split", True)
        max_video_size_mb = self.get_config("bilibili.max_video_size_mb", 100)
        enable_compression = self.get_config("bilibili.enable_video_compression", True)
        compression_quality = self.get_config("bilibili.compression_quality", 23)
        
        logger.debug(f"视频处理配置: 分块={enable_splitting}, 压缩={enable_compression}, 最大大小={max_video_size_mb}MB, 压缩质量={compression_quality}")
        
        # 新的处理策略：先分块后压缩
        # 分块条件：启用分块 AND FFmpeg可用 AND (文件过大 OR 时长过长)
        should_split = (bool(enable_splitting) and 
                       ffmpeg_info["ffmpeg_available"] and 
                       (video_size_mb > max_video_size_mb or (video_duration and video_duration > 300)))  # 5分钟
        
        if enable_splitting and not ffmpeg_info["ffmpeg_available"]:
            logger.warning("分块开关已开启但FFmpeg不可用，将跳过分块处理")

        logger.info(f"处理策略: should_split={should_split} (文件大小={video_size_mb:.2f}MB, 时长={video_duration}秒)")
        
        if should_split:
            logger.info("采用先分块后压缩的处理策略")
            
            # 创建分块器 - 使用跨平台FFmpeg管理器
            ffmpeg_info = _ffmpeg_manager.check_ffmpeg_availability()
            logger.debug(f"FFmpeg可用性检查: {ffmpeg_info}")

            if not ffmpeg_info["ffmpeg_available"]:
                logger.error("系统中未找到FFmpeg，无法进行视频分片")
                should_split = False
            else:
                # 使用FFmpeg管理器获取的路径
                splitter = VideoSplitter(ffmpeg_info["ffmpeg_path"])
                logger.debug(f"使用FFmpeg路径: {ffmpeg_info['ffmpeg_path']}")
            
            # 准备插件内的持久分块目录：插件目录/data/split
            split_dir = _prepare_split_dir()

            # 第一步：进行分块（不考虑文件大小，优先按时间分割）
            if video_duration and video_duration > 300:  # 5分钟
                # 时长过长，使用固定时间分割
                logger.info("使用基于时间的固定分割（每3分钟）")
                split_files = splitter.split_video(temp_path, split_dir)
            else:
                # 文件过大但时长不长，使用智能分割
                logger.info("使用基于文件大小的智能分割")
                split_files = splitter.split_video_by_size(temp_path, split_dir, max_video_size_mb)
            
            if split_files:
                logger.debug(f"视频分块完成，共{len(split_files)}个片段")
                
                # 第二步：检查并压缩超大的分片
                if enable_compression and ffmpeg_info["ffmpeg_available"]:
                    logger.info("开始检查分片并压缩超大文件...")
                    compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"])
                    
                    final_split_files = []
                    compression_stats = {"compressed": 0, "skipped": 0, "failed": 0}
                    
                    for i, part_path in enumerate(split_files):
                        part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                        logger.debug(f"检查分片{i+1}: {part_size_mb:.2f}MB")
                        
                        if part_size_mb > max_video_size_mb:
                            # 需要压缩的分片
                            logger.info(f"分片{i+1}大小({part_size_mb:.2f}MB)超过限制，开始压缩...")
                            compressed_part_path = part_path.replace('.mp4', '_compressed.mp4')
                            
                            if compressor.compress_video(part_path, compressed_part_path, max_video_size_mb, compression_quality):
                                compressed_size_mb = os.path.getsize(compressed_part_path) / (1024 * 1024)
                                logger.info(f"分片{i+1}压缩成功: {part_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB")
                                final_split_files.append(compressed_part_path)
                                compression_stats["compressed"] += 1
                                
                                # 删除原始分片（如果配置允许）
                                if delete_original:
                                    try:
                                        os.remove(part_path)
                                        logger.debug(f"已删除原始分片: {part_path}")
                                    except Exception as e:
                                        logger.warning(f"删除原始分片失败: {e}")
                            else:
                                logger.warning(f"分片{i+1}压缩失败，使用原始文件")
                                final_split_files.append(part_path)
                                compression_stats["failed"] += 1
                        else:
                            # 大小符合要求的分片
                            logger.debug(f"分片{i+1}大小符合要求，无需压缩")
                            final_split_files.append(part_path)
                            compression_stats["skipped"] += 1
                    
                    logger.info(f"分片压缩完成: 压缩{compression_stats['compressed']}个, 跳过{compression_stats['skipped']}个, 失败{compression_stats['failed']}个")
                    split_files = final_split_files
                else:
                    logger.debug("压缩功能已禁用或FFmpeg不可用，跳过分片压缩")
                
                # 在发送所有视频之前，统一进行WSL路径转换
                enable_conversion = self.get_config("wsl.enable_path_conversion", True)
                if enable_conversion:
                    logger.info("开始对所有视频分片进行WSL路径转换")
                    converted_split_files = []
                    for part_path in split_files:
                        converted_path = convert_windows_to_wsl_path(part_path)
                        converted_split_files.append(converted_path)
                        logger.debug(f"路径转换: {part_path} -> {converted_path}")
                else:
                    logger.info("WSL路径转换已禁用，使用原始路径")
                    converted_split_files = split_files
                
                # 最终检查所有分片的文件大小
                logger.info("最终检查分片文件大小...")
                oversized_parts = []
                total_size = 0
                for i, part_path in enumerate(split_files):
                    part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                    total_size += part_size_mb
                    logger.debug(f"最终分片{i+1}大小: {part_size_mb:.2f}MB")
                    if part_size_mb > max_video_size_mb:
                        oversized_parts.append((i+1, part_size_mb))
                
                logger.info(f"分片处理完成: 共{len(split_files)}个分片, 总大小{total_size:.2f}MB")
                
                if oversized_parts:
                    logger.warning(f"仍有{len(oversized_parts)}个超大分片:")
                    for part_num, size_mb in oversized_parts:
                        logger.warning(f"  分片{part_num}: {size_mb:.2f}MB (超过限制{max_video_size_mb}MB)")
                    logger.warning("这些分片可能发送失败")
                else:
                    logger.info("所有分片文件大小符合要求")
                
                # 发送分块后的视频片段
                sent_count = 0
                failed_files = []
                for i, (original_path, converted_path) in enumerate(zip(split_files, converted_split_files)):
                    part_caption = f"{caption} - 第{i+1}部分"
                    
                    if await self._send_video_part(original_path, converted_path, part_caption, stream_id, message):
                        sent_count += 1
                        logger.debug(f"第{i+1}部分发送成功")
                    else:
                        logger.warning(f"第{i+1}部分发送失败")
                        failed_files.append(original_path)
                # 不删除分块文件与目录，保留给外部发送软件使用；将于下一次下载前清理
                
                # 根据配置决定是否删除原始下载文件
                if delete_original:
                    try:
                        os.remove(temp_path)
                        logger.info("已删除原始下载文件")
                    except Exception as e:
                        logger.warning(f"删除原始文件失败: {e}")
                else:
                    logger.debug("保留原始下载文件")
                
                logger.info(f"视频分块发送完成，成功发送{sent_count}/{len(split_files)}个片段")
                return True, True, f"已发送分块视频（{sent_count}个片段）"
            else:
                logger.warning("视频分块失败，将发送原始视频")
                # 不在此处清理目录，保持一致性；下一次下载前会清理
                should_split = False
        
        if not should_split:
            # 处理单个视频文件（不分块）
            final_video_path = temp_path
            
            # 如果文件过大且启用压缩，先压缩
            if (video_size_mb > max_video_size_mb and 
                enable_compression and 
                ffmpeg_info["ffmpeg_available"]):
                logger.info(f"单个视频文件大小({video_size_mb:.2f}MB)超过限制，开始压缩...")
                
                compressed_path = temp_path.replace('.mp4', '_compressed.mp4')
                compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"])
                
                if compressor.compress_video(temp_path, compressed_path, max_video_size_mb, compression_quality):
                    compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                    logger.info(f"单个视频压缩成功: {video_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB")
                    final_video_path = compressed_path
                    
                    # 删除原始文件（如果配置允许）
                    if delete_original:
                        try:
                            os.remove(temp_path)
                            logger.debug(f"已删除原始视频文件: {temp_path}")
                        except Exception as e:
                            logger.warning(f"删除原始视频文件失败: {e}")
                else:
                    logger.warning("单个视频压缩失败，使用原始文件")
            elif video_size_mb > max_video_size_mb:
                logger.warning(f"单个视频文件大小({video_size_mb:.2f}MB)超过限制但压缩功能不可用")
            else:
                logger.debug(f"单个视频文件大小({video_size_mb:.2f}MB)符合要求，无需压缩")
            
            # 发送处理后的视频文件
            async def _try_send(path: str) -> bool:
                # 在发送前进行WSL路径转换
                enable_conversion = self.get_config("wsl.enable_path_conversion", True)
                converted_path = convert_windows_to_wsl_path(path) if enable_conversion else path
                
                logger.debug(f"单视频发送 - 路径转换启用: {enable_conversion}")
                logger.debug(f"单视频发送 - 原始路径: {path}")
                logger.debug(f"单视频发送 - 转换后路径: {converted_path}")
                
                # 检查是否为私聊消息
                is_private = self._is_private_message(message)
                
                if is_private:
                    # 私聊消息，使用专用API发送
                    user_id = self._get_user_id(message)
                    if user_id:
                        logger.info(f"检测到私聊消息，使用私聊视频API发送给用户: {user_id}")
                        return await self._send_private_video(path, converted_path, user_id)
                    else:
                        logger.error("私聊消息但无法获取用户ID")
                        return False
                else:
                    # 群聊消息，使用原有逻辑
                    logger.info("检测到群聊消息，使用原有发送逻辑")
                    
                    # 优先尝试：将本地路径转换为 file:// URI，并以 videourl 形式发送视频
                    try:
                        file_uri = f"file://{converted_path}"
                        
                        logger.info(f"发送URI: {file_uri}")
                        
                        if await send_api.custom_to_stream("videourl", file_uri, stream_id, display_message=caption):
                            logger.info("视频(路径)发送成功")
                            return True
                    except Exception as e:
                        logger.warning(f"视频(路径)发送失败: {e}")

                    # 回退：使用base64作为视频数据发送
                    try:
                        import base64
                        with open(path, 'rb') as video_file:
                            video_data = video_file.read()
                            video_base64 = base64.b64encode(video_data).decode('utf-8')
                        logger.debug(f"视频文件已转换为base64，长度: {len(video_base64)} 字符")
                        if await send_api.custom_to_stream("video", video_base64, stream_id, display_message=caption):
                            logger.info("视频(baes64)发送成功")
                            return True
                    except Exception as e:
                        logger.warning(f"视频(baes64)发送失败: {e}")

                    return False

            sent_ok = await _try_send(final_video_path)
            if not sent_ok:
                logger.warning("所有发送方式都失败，发送提示信息")
                await self._send_text("视频解析成功，但宿主暂不支持直接发送视频文件。", stream_id)
            else:
                logger.info("视频文件发送成功")
            
            # 删除临时文件
            try:
                # 删除最终处理的文件
                if os.path.exists(final_video_path):
                    os.remove(final_video_path)
                    logger.debug(f"已删除处理后的视频文件: {final_video_path}")
                
                # 如果还有原始文件且不同于最终文件，也删除
                if final_video_path != temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                    logger.debug(f"已删除原始视频文件: {temp_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {e}")
            
        logger.info("B站视频处理完成")
        return True, True, "已发送视频（若宿主支持）"

    async def _send_video_part(self, original_path: str, converted_path: str, caption: str, stream_id: str, message: MaiMessages) -> bool:
        """发送视频分块片段
        
        Args:
            original_path: 原始文件路径（用于文件检查和base64读取）
            converted_path: 转换后的路径（用于发送URI）
            caption: 视频标题
            stream_id: 流ID
            message: 消息对象
        """
        try:
            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                logger.error(f"视频分片文件不存在: {original_path}")
                return False
                
            # 检查文件大小（使用原始路径）
            file_size = os.path.getsize(original_path)
            if file_size == 0:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                logger.error(f"视频分片文件大小为0: {original_path}")
                return False
                
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.debug(f"准备发送视频分片: {original_path} -> {converted_path}, 大小: {file_size} 字节")

            # 检查是否为私聊消息
            is_private = self._is_private_message(message)
            
            if is_private:
                # 私聊消息，使用专用API发送
                user_id = self._get_user_id(message)
                if user_id:
                    logger.info(f"分块视频检测到私聊消息，使用私聊视频API发送给用户: {user_id}")
                    return await self._send_private_video(original_path, converted_path, user_id)
                else:
                    logger.error("私聊消息但无法获取用户ID")
                    return False
            else:
                # 群聊消息，使用原有逻辑
                logger.info("分块视频检测到群聊消息，使用原有发送逻辑")
                
                # 优先尝试：构造 file:// URI 并以 videourl 形式发送（使用转换后路径）
                try:
                    file_uri = f"file://{converted_path}"
                    
                    logger.info(f"原始路径: {original_path}")
                    logger.info(f"转换后路径: {converted_path}")
                    logger.info(f"发送URI: {file_uri}")
                    
                    if await send_api.custom_to_stream("videourl", file_uri, stream_id, display_message=caption):
                        logger.debug(f"视频分片(路径)发送成功: {original_path}")
                        return True
                except Exception as e:
                    logger.warning(f"视频分片(路径)发送失败: {e}")

                # 回退：转换为base64并以视频形式发送（使用原始路径）
                try:
                    import base64
                    with open(original_path, 'rb') as video_file:
                        video_data = video_file.read()
                        video_base64 = base64.b64encode(video_data).decode('utf-8')
                    logger.debug(f"视频文件已转换为base64，长度: {len(video_base64)} 字符")
                    if await send_api.custom_to_stream("video", video_base64, stream_id, display_message=caption):
                        logger.debug(f"视频分片发送成功: {original_path}")
                        return True
                except Exception as e:
                    logger.warning(f"视频分片(base64)发送失败: {e}")
                
        except Exception as e:
            from src.common.logger import get_logger
            logger = get_logger("bilibili_handler")
            logger.warning(f"视频片段发送失败: {e}")

        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        logger.error(f"所有发送方式都失败: {original_path}")
        return False

@register_plugin
class BilibiliVideoSenderPlugin(BasePlugin):
    """B站视频解析与自动发送插件。"""

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
        },
        "bilibili": {
            "use_wbi": ConfigField(type=bool, default=True, description="是否使用WBI签名（推荐开启）"),
            "prefer_dash": ConfigField(type=bool, default=True, description="是否优先使用DASH格式（推荐开启）"),
            "fnval": ConfigField(type=int, default=4048, description="视频流格式标识（1=MP4, 16=FLV, 80=DASH, 64=MP4+DASH, 32=MP4+FLV+DASH, 128=MP4+FLV+DASH+8K, 256=MP4+FLV+DASH+8K+HDR, 512=MP4+FLV+DASH+8K+HDR+杜比, 1024=MP4+FLV+DASH+8K+HDR+杜比+AV1, 2048=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度, 4096=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度+8K360度, 8192=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度+8K360度+HDR360度）"),
            "fourk": ConfigField(type=bool, default=True, description="是否允许4K视频（需要大会员）"),
            "qn": ConfigField(type=int, default=0, description="视频清晰度选择（0=自动, 6=240P, 16=360P, 32=480P, 64=720P, 80=1080P, 112=1080P+, 116=1080P60, 120=4K, 125=HDR, 126=杜比视界）"),
            "platform": ConfigField(type=str, default="pc", description="平台类型（pc=web播放, html5=移动端HTML5播放）"),
            "high_quality": ConfigField(type=bool, default=False, description="是否启用高画质模式（platform=html5时有效）"),
            "try_look": ConfigField(type=bool, default=False, description="是否启用游客高画质尝试模式（未登录时可能获取720P和1080P）"),
            "sessdata": ConfigField(type=str, default="", description="B站登录Cookie中的SESSDATA值（用于获取高清晰度视频）"),
            "buvid3": ConfigField(type=str, default="", description="B站设备标识Buvid3（用于生成session参数）"),
            "enable_video_splitting": ConfigField(type=bool, default=True, description="是否启用视频分块功能"),
            "delete_original_after_split": ConfigField(type=bool, default=True, description="是否在分块后删除原始文件"),
            "max_video_size_mb": ConfigField(type=int, default=100, description="视频文件大小限制（MB），超过此大小将进行压缩或分割"),
            "enable_video_compression": ConfigField(type=bool, default=True, description="是否启用视频压缩功能"),
            "compression_quality": ConfigField(type=int, default=23, description="视频压缩质量 (1-51，数值越小质量越高，推荐18-28)"),
        },
        "ffmpeg": {
            "show_warnings": ConfigField(type=bool, default=True, description="是否显示FFmpeg相关警告信息"),
        },
        "wsl": {
            "enable_path_conversion": ConfigField(type=bool, default=True, description="是否启用Windows到WSL的路径转换"),
        },
        "api": {
            "port": ConfigField(type=int, default=5700, description="API服务端口号"),
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (BilibiliAutoSendHandler.get_handler_info(), BilibiliAutoSendHandler),
        ]


