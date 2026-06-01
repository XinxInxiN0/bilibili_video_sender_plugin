# -*- coding: utf-8 -*-
"""视频发送工具：通过 SDK 或 OneBot HTTP API 发送视频消息。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

import aiohttp

from .utils import convert_windows_to_wsl_path

if TYPE_CHECKING:
    from ..plugin import ApiConfig

_logger = logging.getLogger("plugin.bilibili_video_sender.sender")
_NAPCAT_VIDEO_LIMIT_BYTES = 100 * 1024 * 1024


async def send_text(
    ctx: Any,
    content: str,
    stream_id: str,
    message: dict[str, Any],
    api_config: ApiConfig,
) -> bool:
    """通过 OneBot HTTP API 发送文本消息。

    SDK 路径（ctx.send.text）与视频发送同样存在静默失败问题，
    直接走 OneBot HTTP API 保证可靠投递。
    """
    return await _send_text_via_onebot(content, message, api_config)


async def send_video(
    ctx: Any,
    original_path: str,
    runtime_mode: str,
    message: dict[str, Any],
    api_config: ApiConfig,
) -> bool:
    """发送视频文件。

    根据消息类型（私聊/群聊）和运行环境模式选择合适的发送方式。
    优先使用 SDK 的 send.custom，失败时 fallback 到 OneBot HTTP API。
    """
    converted_path = convert_windows_to_wsl_path(original_path) if runtime_mode == "wsl" else original_path

    _logger.debug("Sending video - runtime mode: %s", runtime_mode)
    _logger.debug("Sending video - original path: %s", original_path)
    _logger.debug("Sending video - converted path: %s", converted_path)

    if not os.path.exists(original_path):
        _logger.error("视频文件不存在: %s", original_path)
        return False

    file_size = os.path.getsize(original_path)
    if file_size > _NAPCAT_VIDEO_LIMIT_BYTES:
        _logger.info(
            "Video file exceeds NapCat video limit (%.2f MiB > 100 MiB), uploading as file",
            file_size / (1024 * 1024),
        )
        return await _upload_file_via_onebot(original_path, converted_path, message, api_config)

    # MaiBot send_service 对 "video" custom type 无原生支持，会降级为 DictComponent，
    # 导致 QQ OneBot 适配器无法识别而静默丢弃消息（ctx.send.custom 不抛异常仍返回 True）。
    # 直接走 OneBot HTTP API，与旧版本行为一致。
    if await _send_via_onebot(converted_path, message, api_config):
        return True

    _logger.warning("Video message sending failed, falling back to file upload")
    return await _upload_file_via_onebot(original_path, converted_path, message, api_config)


async def _send_text_via_onebot(
    content: str,
    message: dict[str, Any],
    api_config: ApiConfig,
) -> bool:
    """直接通过 OneBot HTTP API 发送文本消息。"""
    try:
        host = api_config.host
        port = api_config.port
        token = str(api_config.token).strip()

        is_private = _is_private_message(message)
        if is_private:
            user_id = _get_user_id(message)
            if not user_id:
                _logger.error("私聊消息但无法获取用户 ID")
                return False
            api_url = f"http://{host}:{port}/send_private_msg"
            request_data = {"user_id": user_id, "message": [{"type": "text", "data": {"text": content}}]}
        else:
            group_id = _get_group_id(message)
            if not group_id:
                _logger.error("群聊消息但无法获取群 ID")
                return False
            api_url = f"http://{host}:{port}/send_group_msg"
            request_data = {"group_id": group_id, "message": [{"type": "text", "data": {"text": content}}]}

        _logger.debug("OneBot text API: %s", api_url)

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=request_data, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return True

                if response.status in (401, 403) and token:
                    _logger.warning("OneBot auth failed (%d), retrying with access_token", response.status)
                    retry_url = f"{api_url}?access_token={urllib.parse.quote(token)}"
                    async with session.post(retry_url, json=request_data, headers=headers, timeout=30) as retry_resp:
                        if retry_resp.status == 200:
                            return True
                        error_text = await retry_resp.text()
                        _logger.error("Failed to send text (retry): HTTP %d, %s", retry_resp.status, error_text)
                        return False

                error_text = await response.text()
                _logger.error("Failed to send text: HTTP %d, %s", response.status, error_text)
                return False

    except asyncio.TimeoutError:
        _logger.error("Text sending timeout")
        return False
    except Exception as e:
        _logger.error("Text sending error: %s", e)
        return False


async def _send_via_sdk(ctx: Any, file_uri: str, message: dict[str, Any]) -> bool:
    """通过 SDK 的 send.custom 发送视频。"""
    try:
        session_id = message.get("session_id", "")
        if not session_id:
            _logger.warning("No session_id in message, cannot send via SDK")
            return False

        result = await ctx.send.custom(
            custom_type="video",
            data={"file": file_uri},
            stream_id=session_id,
        )
        _logger.debug("SDK video send result: %s", result)
        return bool(result)
    except Exception as e:
        _logger.warning("SDK video send failed: %s", e)
        return False


async def _send_via_onebot(
    converted_path: str,
    message: dict[str, Any],
    api_config: ApiConfig,
) -> bool:
    """直接通过 OneBot HTTP API 发送视频。"""
    host = api_config.host
    port = api_config.port
    token = str(api_config.token).strip()
    file_uri = _as_file_uri(converted_path)

    is_private = _is_private_message(message)
    if is_private:
        user_id = _get_user_id(message)
        if not user_id:
            _logger.error("Private message but unable to get user ID")
            return False
        api_url = f"http://{host}:{port}/send_private_msg"
        request_data = {"user_id": user_id, "message": [{"type": "video", "data": {"file": file_uri}}]}
    else:
        group_id = _get_group_id(message)
        if not group_id:
            _logger.error("Group message but unable to get group ID")
            return False
        api_url = f"http://{host}:{port}/send_group_msg"
        request_data = {"group_id": group_id, "message": [{"type": "video", "data": {"file": file_uri}}]}

    _logger.debug("OneBot video API: %s, data: %s", api_url, request_data)
    return await _send_onebot_request(api_url, request_data, token, 300, "video")


async def _upload_file_via_onebot(
    original_path: str,
    converted_path: str,
    message: dict[str, Any],
    api_config: ApiConfig,
) -> bool:
    """通过 OneBot 文件上传动作发送文件。"""
    host = api_config.host
    port = api_config.port
    token = str(api_config.token).strip()
    file_uri = _as_file_uri(converted_path)
    file_name = os.path.basename(original_path) or "bilibili_video.mp4"

    is_private = _is_private_message(message)
    if is_private:
        user_id = _get_user_id(message)
        if not user_id:
            _logger.error("Private message but unable to get user ID")
            return False
        api_url = f"http://{host}:{port}/upload_private_file"
        request_data = {"user_id": user_id, "file": file_uri, "name": file_name, "upload_file": True}
    else:
        group_id = _get_group_id(message)
        if not group_id:
            _logger.error("Group message but unable to get group ID")
            return False
        api_url = f"http://{host}:{port}/upload_group_file"
        request_data = {"group_id": group_id, "file": file_uri, "name": file_name, "upload_file": True}

    _logger.debug("OneBot file API: %s, data: %s", api_url, request_data)
    return await _send_onebot_request(api_url, request_data, token, 300, "file")


async def _send_onebot_request(
    api_url: str,
    request_data: dict[str, Any],
    token: str,
    timeout: int,
    action_name: str,
) -> bool:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession() as session:
            status, body = await _post_onebot(session, api_url, request_data, headers, timeout)
            if status in (401, 403) and token:
                _logger.warning("OneBot auth failed (%d), retrying with access_token", status)
                retry_url = f"{api_url}?access_token={urllib.parse.quote(token)}"
                status, body = await _post_onebot(session, retry_url, request_data, headers, timeout)
            return _onebot_result_ok(action_name, status, body)
    except asyncio.TimeoutError:
        _logger.error("OneBot %s sending timeout", action_name)
        return False
    except Exception as e:
        _logger.error("OneBot %s sending error: %s", action_name, e)
        return False


async def _post_onebot(
    session: aiohttp.ClientSession,
    api_url: str,
    request_data: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> tuple[int, str]:
    async with session.post(api_url, json=request_data, headers=headers, timeout=timeout) as response:
        return response.status, await response.text()


def _onebot_result_ok(action_name: str, status: int, body: str) -> bool:
    if status != 200:
        _logger.error("Failed to send %s: HTTP %d, %s", action_name, status, body)
        return False

    try:
        result = json.loads(body) if body else {}
    except json.JSONDecodeError:
        _logger.error("Failed to send %s: invalid OneBot response %s", action_name, body)
        return False

    if result.get("status") == "ok" and result.get("retcode") in (0, "0"):
        _logger.debug("%s sent successfully via OneBot: %s", action_name.capitalize(), result)
        return True

    _logger.error("Failed to send %s: %s", action_name, result)
    return False


def _as_file_uri(path: str) -> str:
    if path.startswith(("http://", "https://", "file://")):
        return path
    return "file:///" + urllib.request.pathname2url(path).lstrip("/")


def _is_private_message(message: dict[str, Any]) -> bool:
    """检测消息是否为私聊消息。

    SDK MessageDict 中私聊消息的 message_info.group_info 为 None，群聊则有值。
    """
    message_info = message.get("message_info", {})
    if not message_info:
        return False
    return message_info.get("group_info") is None


def _get_user_id(message: dict[str, Any]) -> str | None:
    """从消息中获取用户 ID。"""
    message_info = message.get("message_info", {})
    if not message_info:
        return None
    user_info = message_info.get("user_info", {})
    if not user_info:
        return None
    user_id = user_info.get("user_id")
    return str(user_id) if user_id else None


def _get_group_id(message: dict[str, Any]) -> str | None:
    """从消息中获取群 ID。"""
    message_info = message.get("message_info", {})
    if not message_info:
        return None
    group_info = message_info.get("group_info")
    if not group_info:
        return None
    group_id = group_info.get("group_id")
    return str(group_id) if group_id else None
