# -*- coding: utf-8 -*-
"""工具函数：路径转换、进度条、Docker 检测、临时目录管理。"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time

_logger = logging.getLogger("plugin.bilibili_video_sender.utils")


def get_plugin_root_dir() -> str:
    """返回插件根目录，兼容 core 子包内的路径推导。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker 容器内。"""
    if os.path.exists("/.dockerenv"):
        return True
    cgroup_path = "/proc/1/cgroup"
    try:
        with open(cgroup_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return any(keyword in content for keyword in ("docker", "kubepods", "containerd"))
    except FileNotFoundError:
        return False
    except Exception:
        return False


def get_download_temp_dir(linux_temp_dir: str = "") -> str:
    """获取下载临时目录：优先使用共享目录，确保跨进程访问。"""
    if is_running_in_docker():
        return "/MaiMBot/data/tmp"

    if platform.system().lower() == "linux" and str(linux_temp_dir).strip():
        return os.path.abspath(os.path.expanduser(str(linux_temp_dir).strip()))

    return os.path.join(get_plugin_root_dir(), "tmp")


def ensure_shared_file_permissions(file_path: str) -> None:
    """确保下载产物可被 NapCat 等独立进程读取。"""
    if platform.system().lower() == "windows":
        return

    try:
        parent_dir = os.path.dirname(file_path)
        if parent_dir and os.path.isdir(parent_dir):
            os.chmod(parent_dir, 0o755)
        if os.path.isfile(file_path):
            os.chmod(file_path, 0o644)
    except Exception as e:
        _logger.warning("Failed to update shared file permissions: %s", e)


def convert_windows_to_wsl_path(windows_path: str) -> str:
    """将 Windows 路径转换为 WSL 路径。

    例如：E:\\path\\to\\file.mp4 -> /mnt/e/path/to/file.mp4
    """
    try:
        try:
            result = subprocess.run(
                ["wsl", "wslpath", "-u", windows_path],
                capture_output=True,
                text=False,
                check=True,
            )
            wsl_path = result.stdout.decode("utf-8", errors="replace").strip()
            if wsl_path:
                return wsl_path
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        if re.match(r"^[a-zA-Z]:", windows_path):
            drive = windows_path[0].lower()
            path = windows_path[2:].replace("\\", "/").lstrip("/")
            return f"/mnt/{drive}/{path}"
        return windows_path
    except Exception:
        return windows_path


class ProgressBar:
    """进度条显示类。"""

    def __init__(self, total_size: int, description: str = "下载进度", bar_length: int = 30):
        self.total_size = total_size
        self.description = description
        self.bar_length = bar_length
        self.current_size = 0
        self.last_update = 0.0
        self.update_interval = 0.1

    def update(self, downloaded: int) -> None:
        """更新进度。"""
        self.current_size = downloaded
        current_time = time.time()

        if current_time - self.last_update < self.update_interval:
            return

        self.last_update = current_time

        if self.total_size > 0:
            percentage = (downloaded / self.total_size) * 100
        else:
            percentage = 0

        filled_length = int(self.bar_length * downloaded // self.total_size) if self.total_size > 0 else 0
        bar = "#" * filled_length + "-" * (self.bar_length - filled_length)  # ASCII-safe for Windows GBK console

        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024) if self.total_size > 0 else 0

        print(
            f"\r{self.description}: [{bar}] {percentage:5.1f}% ({downloaded_mb:6.1f}MB/{total_mb:6.1f}MB)",
            end="",
            flush=True,
        )

    def finish(self) -> None:
        """完成进度条显示。"""
        self.update(self.total_size)
        print()


def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符。"""
    return re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or "bilibili_video"
