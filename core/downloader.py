# -*- coding: utf-8 -*-
"""视频下载与 DASH 合并逻辑。"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .auth import build_cookie_header, normalize_credentials
from .ffmpeg import ffmpeg_manager
from .parser import BilibiliParser
from .utils import ProgressBar, get_download_temp_dir, sanitize_filename

_logger = logging.getLogger("plugin.bilibili_video_sender.downloader")


def _download_stream(
    url_list: List[str],
    save_path: str,
    desc: str,
    headers: Dict[str, str],
) -> bool:
    """下载单条流，支持多 URL 自动切换（主链失败时自动切备链）。"""
    if not url_list:
        return False
    last_err = None
    for idx, url in enumerate(url_list, start=1):
        try:
            req = BilibiliParser._build_request(url, headers)
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec
                total_size = resp.headers.get("content-length")
                total_size = int(total_size) if total_size else 0

                progress_bar = ProgressBar(total_size, f"{desc} (候选{idx}/{len(url_list)})", 30)
                with open(save_path, "wb") as f:
                    downloaded = 0
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        progress_bar.update(downloaded)

                progress_bar.finish()
                if total_size > 0 and downloaded < total_size:
                    raise IOError(f"下载不完整: {downloaded}/{total_size}")
            return True
        except Exception as e:
            last_err = e
            _logger.warning("%s 第 %d 条链接下载失败: %s", desc, idx, e)
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except Exception:
                pass
    if last_err:
        _logger.error("%s 所有链接下载失败: %s", desc, last_err)
    return False


def _merge_dash_video_audio(
    video_temp: str,
    audio_temp: Optional[str],
    output_path: str,
    ffmpeg_path: str,
) -> bool:
    """使用 FFmpeg 合并 DASH 视频和音频流。"""
    try:
        ffprobe_path = ffmpeg_manager.get_ffprobe_path()
        video_format = "unknown"
        if ffprobe_path:
            probe_cmd = [
                ffprobe_path,
                "-v", "error",
                "-show_entries", "format=format_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_temp,
            ]
            try:
                video_format = (
                    subprocess.run(probe_cmd, capture_output=True, text=False)
                    .stdout.decode("utf-8", errors="replace")
                    .strip()
                )
            except Exception as e:
                _logger.warning("Unable to check video format: %s", e)

        has_audio = audio_temp and os.path.exists(audio_temp)

        if "m4s" in video_format.lower() or video_temp.lower().endswith(".m4s"):
            if has_audio:
                ffmpeg_cmd = [
                    ffmpeg_path,
                    "-i", video_temp,
                    "-i", audio_temp,
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-strict", "experimental",
                    "-b:a", "192k",
                    "-y", output_path,
                ]
            else:
                ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", output_path]
        else:
            if has_audio:
                ffmpeg_cmd = [
                    ffmpeg_path,
                    "-i", video_temp,
                    "-i", audio_temp,
                    "-c:v", "copy",
                    "-c:a", "copy",
                    "-y", output_path,
                ]
            else:
                ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", output_path]

        _logger.debug("Starting to merge video and audio...")
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=False)
        if result.returncode == 0:
            _logger.debug("Video and audio merged successfully")
            # 清理临时文件
            try:
                if os.path.exists(video_temp):
                    os.remove(video_temp)
                if has_audio:
                    os.remove(audio_temp)
                _logger.debug("Temporary files cleaned")
            except Exception as e:
                _logger.warning("Failed to clean temp: %s", e)
            return True

        stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        _logger.warning("FFmpeg merge failed: %s", stderr_text)

        # 合并失败时退化为仅视频转封装
        fallback_cmd = [ffmpeg_path, "-i", video_temp, "-c", "copy", "-y", output_path]
        fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=False)
        if fallback_result.returncode == 0:
            _logger.warning("Audio merge failed, fallback to video-only mp4")
            try:
                if os.path.exists(video_temp):
                    os.remove(video_temp)
                if has_audio:
                    os.remove(audio_temp)
            except Exception:
                pass
            return True

        fallback_err = fallback_result.stderr.decode("utf-8", errors="replace") if fallback_result.stderr else ""
        _logger.warning("FFmpeg fallback remux failed: %s", fallback_err)
        return False
    except Exception as e:
        _logger.warning("Merge failed: %s", e)
        return False


def _remux_to_mp4(input_path: str, output_path: str, ffmpeg_path: str) -> bool:
    """使用 FFmpeg 转封装为 mp4。"""
    remux_cmd = [ffmpeg_path, "-i", input_path, "-c", "copy", "-y", output_path]
    remux_result = subprocess.run(remux_cmd, capture_output=True, text=False)
    if remux_result.returncode == 0:
        _logger.debug("Single file remuxed to mp4")
        try:
            os.remove(input_path)
        except Exception:
            pass
        return True

    stderr_text = remux_result.stderr.decode("utf-8", errors="replace") if remux_result.stderr else ""
    _logger.warning("Single file remux failed: %s", stderr_text)
    return False


def download_video(
    info: Any,  # BilibiliVideoInfo
    sources: Dict[str, Any],
    credentials: Dict[str, Any] | str,
    linux_temp_dir: str = "",
) -> Optional[str]:
    """下载视频并合并为 mp4 文件（阻塞函数，应在线程池中运行）。

    Returns:
        临时文件路径，失败返回 None。
    """
    try:
        safe_title = sanitize_filename(info.title)
        unique_tag = f"{info.aid}_{info.cid}_{int(time.time() * 1000)}"
        base_name = f"{safe_title}_{unique_tag}"
        tmp_dir = get_download_temp_dir(linux_temp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        temp_path = os.path.join(tmp_dir, f"{base_name}.mp4")

        _logger.debug("Preparing download: title=%s, temp=%s", info.title, temp_path)

        # 构建下载请求头
        headers = {
            "User-Agent": BilibiliParser.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Range": "bytes=0-",
        }
        if isinstance(credentials, str):
            cookie_header = credentials.strip()
        else:
            cookie_header = build_cookie_header(normalize_credentials(credentials))
        if cookie_header:
            headers["Cookie"] = cookie_header
            _logger.debug("Cookie auth added for download")
        else:
            _logger.debug("No Cookie for download, may get 403 error")

        source_type = sources.get("type")

        # DASH 格式
        if source_type == "dash":
            video_urls = sources.get("video_urls") or []
            audio_urls = sources.get("audio_urls") or []

            _logger.debug("DASH format assumed: video_urls=%d, audio_urls=%d", len(video_urls), len(audio_urls))

            video_temp = os.path.join(tmp_dir, f"{base_name}_video.m4s")
            if not _download_stream(video_urls, video_temp, "Video stream downloading", headers):
                return None

            audio_temp: Optional[str] = None
            if audio_urls:
                audio_temp = os.path.join(tmp_dir, f"{base_name}_audio.m4s")
                if not _download_stream(audio_urls, audio_temp, "Audio stream downloading", headers):
                    _logger.warning("Audio stream download failed, continue with video only")
                    audio_temp = None

            ffmpeg_path = ffmpeg_manager.get_ffmpeg_path()
            if ffmpeg_path:
                _logger.debug("Using FFmpeg: %s", ffmpeg_path)
                if _merge_dash_video_audio(video_temp, audio_temp, temp_path, ffmpeg_path):
                    return temp_path
            else:
                _logger.warning("FFmpeg not found, cannot merge video and audio")

            # 清理失败时的临时文件
            _logger.warning("DASH 合并失败且无法转封装，放弃发送 m4s")
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

        # durl 格式
        if source_type == "durl":
            url_list = sources.get("urls") or []
            if not url_list:
                return None
            parsed_path = urllib.parse.urlparse(url_list[0]).path
            ext = os.path.splitext(parsed_path)[1].lower()
            download_path = temp_path
            if ext and ext != ".mp4":
                download_path = os.path.join(tmp_dir, f"{base_name}{ext}")

            _logger.debug("Single file download: path=%s", download_path)
            if not _download_stream(url_list, download_path, "Video downloading", headers):
                return None

            final_path = download_path
            if ext and ext != ".mp4":
                ffmpeg_path = ffmpeg_manager.get_ffmpeg_path()
                if ffmpeg_path:
                    remux_path = os.path.join(tmp_dir, f"{base_name}.mp4")
                    if _remux_to_mp4(download_path, remux_path, ffmpeg_path):
                        final_path = remux_path
                else:
                    _logger.debug("FFmpeg not found, skipping remux")

            return final_path

        _logger.debug("No supported streams found for download")
        return None
    except Exception as e:
        _logger.error("Failed to download video: %s", e)
        return None
