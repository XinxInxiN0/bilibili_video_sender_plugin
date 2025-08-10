from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request

from typing import Any, Dict, List, Optional, Tuple, Type

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

        # 优先请求 DASH
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
            params["session"] = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()

        # WBI 签名
        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )
        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"
        logger.info(f"请求视频信息: {api}")

        # 构建请求头：可带 Cookie
        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)

        # 发起请求
        req = BilibiliParser._build_request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data_bytes = resp.read()
        payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        if payload.get("code") != 0:
            logger.error(f"请求失败: {payload.get('message')}")
            return [], payload.get("message", "接口返回错误")

        data = payload.get("data", {})
        
        # 首先检查是否有durl（单文件格式）
        durl = data.get("durl") or []
        if durl:
            logger.info(f"找到durl格式视频，共{len(durl)}个链接")
            urls = [item.get("url") for item in durl if item.get("url")]
            if urls:
                logger.info(f"成功获取单文件格式视频，共{len(urls)}个链接")
                return urls, "ok"

        # 处理dash格式
        dash = data.get("dash")
        if not dash:
            logger.warning("未找到dash格式数据")
            return [], "未找到dash数据"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            logger.info(f"找到{len(dolby_audios)}个杜比音频流")
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            logger.info(f"找到{len(flac_audios)}个Flac音频流")
        
        # 合并所有音频流
        all_audios = audios + dolby_audios + flac_audios
        
        logger.info(f"获取到{len(videos)}个视频流和{len(all_audios)}个音频流")
        
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
                logger.info(f"添加视频流: {best_video.get('codecs', 'unknown')}, {best_video.get('bandwidth', 0)//1000}kbps")
                
        # 参考原脚本，选择最高质量的音频流
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                logger.info(f"添加音频流: {best_audio.get('codecs', 'unknown')}, {best_audio.get('bandwidth', 0)//1000}kbps")
                
        if candidates:
            return candidates, "ok"
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
        use_wbi = bool(opts.get("use_wbi", True))
        fnval = 4048
        fourk = 1 if bool(opts.get("fourk", True)) else 0
        platform = str(opts.get("platform", "pc"))
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()

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
            params["session"] = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()

        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )
        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"
        logger.info(f"强制请求dash格式: {api}")

        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)

        req = BilibiliParser._build_request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data_bytes = resp.read()
        payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        if payload.get("code") != 0:
            logger.error(f"强制dash请求失败: {payload.get('message')}")
            return [], payload.get("message", "接口返回错误")

        data = payload.get("data", {})
        
        # 检查是否仍然返回durl格式
        durl = data.get("durl")
        if durl:
            logger.info("强制dash请求也返回durl格式，说明该视频只有单文件格式")
            return [], "该视频只有单文件格式"
        
        dash = data.get("dash")
        if not dash:
            logger.warning("强制dash请求也未找到dash数据")
            return [], "未找到dash数据"
        
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        
        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []
        
        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
        
        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
        
        all_audios = audios + dolby_audios + flac_audios
        
        logger.info(f"强制dash请求获取到{len(videos)}个视频流和{len(all_audios)}个音频流")
        
        if not videos or not all_audios:
            logger.warning("强制dash请求中缺少视频或音频流")
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
                logger.info(f"添加dash视频流: {best_video.get('codecs', 'unknown')}, {best_video.get('bandwidth', 0)//1000}kbps")
            
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                logger.info(f"添加dash音频流: {best_audio.get('codecs', 'unknown')}, {best_audio.get('bandwidth', 0)//1000}kbps")
        
        return candidates, "ok" if len(candidates) >= 2 else "未获取到完整的视频和音频流"


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
    """收到包含哔哩哔哩视频链接的消息后，自动解析并发送直链与视频。"""

    event_type = EventType.ON_MESSAGE
    handler_name = "bilibili_auto_send_handler"
    handler_description = "解析B站视频链接并发送直链"

    def _get_stream_id(self, message: MaiMessages) -> str | None:
        """从消息中获取stream_id"""
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
        # 方法1：直接从message对象的stream_id属性获取
        if message.stream_id:
            logger.info(f"方法1成功：直接从message.stream_id获取 - {message.stream_id}")
            return message.stream_id
            
        # 方法2：从chat_stream属性获取
        if hasattr(message, 'chat_stream') and message.chat_stream:
            stream_id = getattr(message.chat_stream, 'stream_id', None)
            if stream_id:
                logger.info(f"方法2成功：从message.chat_stream.stream_id获取 - {stream_id}")
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
                        logger.info(f"方法3成功：从message_base_info生成stream_id - {stream_id}")
                        return stream_id
            except Exception as e:
                logger.error(f"方法3失败：{e}")
        
        # 方法4：从additional_data中查找
        if message.additional_data:
            stream_id = message.additional_data.get("stream_id")
            if stream_id:
                logger.info(f"方法4成功：从additional_data获取stream_id - {stream_id}")
                return stream_id
        
        # 如果所有方法都失败，返回None
        logger.error("所有获取stream_id的方法都失败了")
        return None

    async def _send_text(self, content: str, stream_id: str) -> bool:
        """发送文本消息"""
        try:
            return await send_api.text_to_stream(content, stream_id)
        except Exception as e:
            # 记录错误但不抛出异常，避免影响其他处理器
            return False

    async def execute(self, message: MaiMessages) -> Tuple[bool, bool, str | None]:
        from src.common.logger import get_logger
        logger = get_logger("bilibili_handler")
        
        logger.info(f"BilibiliAutoSendHandler.execute 被调用，消息: {getattr(message, 'raw_message', '')[:30]}...")
        
        if not self.get_config("plugin.enabled", True):
            logger.info("插件已禁用")
            return True, True, None

        raw: str = getattr(message, "raw_message", "") or ""
        logger.info(f"原始消息: {raw[:50]}...")
        
        url = BilibiliParser.find_first_bilibili_url(raw)
        if not url:
            logger.info("未找到B站链接")
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
                    logger.info(f"备选方案：找到平台 {platform} 和用户ID {user_id}")
                    # 创建一个临时的stream_id
                    chat_manager = get_chat_manager()
                    stream_id = chat_manager.get_stream_id(platform, user_id, False)
                    logger.info(f"备选方案：生成临时stream_id {stream_id}")
                else:
                    logger.error("备选方案失败：无法获取平台和用户ID")
                    return True, True, "无法获取聊天流ID"
            except Exception as e:
                logger.error(f"备选方案失败：{e}")
                return True, True, "无法获取聊天流ID"
        
        logger.info(f"获取到stream_id: {stream_id}")

        loop = asyncio.get_running_loop()

        def _blocking() -> Optional[Tuple[BilibiliVideoInfo, List[str], str]]:
            info = BilibiliParser.get_view_info_by_url(url)
            if not info:
                return None
            # 读取配置
            opts = {
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
            }
            urls, status = BilibiliParser.get_play_urls(info.aid, info.cid, opts)
            return info, urls, status

        try:
            result = await loop.run_in_executor(None, _blocking)
        except Exception as exc:  # noqa: BLE001 - 简要兜底
            await self._send_text(f"解析失败：{exc}", stream_id)
            return True, True, "解析失败"

        if not result:
            await self._send_text("未能解析该视频链接，请稍后重试。", stream_id)
            return True, True, "解析失败"

        info, urls, status = result
        if not urls:
            await self._send_text(f"解析失败：{status}", stream_id)
            return True, True, "解析失败"

        # 发送解析结果（标题 + 直链）
        preview = "\n".join(urls[:3])  # 控制数量，避免过长
        text = f"解析成功：\n标题：{info.title}\n直链：\n{preview}"
        await self._send_text(text, stream_id)

        # 同时发送视频文件
        def _download_to_temp() -> Optional[str]:
            try:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                
                safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", info.title).strip() or "bilibili_video"
                tmp_dir = tempfile.gettempdir()
                temp_path = os.path.join(tmp_dir, f"{safe_title}.mp4")
                
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
                
                # 判断是否是分离的视频和音频流
                if len(urls) >= 2 and (".m4s" in urls[0].lower() or ".m4s" in urls[1].lower()):
                    logger.info("检测到分离的视频和音频流，尝试合并")
                    
                    # 下载视频流
                    video_temp = os.path.join(tmp_dir, f"{safe_title}_video.m4s")
                    req = BilibiliParser._build_request(urls[0], headers)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        with open(video_temp, "wb") as f:
                            while True:
                                chunk = resp.read(1024 * 256)
                                if not chunk:
                                    break
                                f.write(chunk)
                    logger.info(f"视频流下载完成: {video_temp}")
                    
                    # 下载音频流
                    audio_temp = os.path.join(tmp_dir, f"{safe_title}_audio.m4s")
                    # 确保有音频URL
                    if len(urls) < 2:
                        logger.warning("没有找到音频流URL，将尝试其他方法")
                        # 尝试重新获取带音频的URL
                        try:
                            # 重新请求带音频的链接
                            params = {
                                "avid": str(info.aid),
                                "cid": str(info.cid),
                                "qn": "64",  # 降低质量以获取单文件
                                "otype": "json",
                                "fourk": "0",
                                "fnver": "0",
                                "fnval": "1",
                                "platform": self.get_config("bilibili.platform", "pc"),
                            }
                            # 尝试使用 WBI 签名
                            use_wbi = self.get_config("bilibili.use_wbi", True)
                            final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
                            query = urllib.parse.urlencode(final_params)
                            api_base = (
                                "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
                            )
                            api = f"{api_base}?{query}"
                            logger.info(f"尝试获取带音频的单文件视频: {api}")
                            # 构造带 Cookie 的请求
                            req = BilibiliParser._build_request(api, headers)
                            with urllib.request.urlopen(req, timeout=15) as resp:
                                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
                            
                            if payload.get("code") == 0:
                                data = payload.get("data", {})
                                durl = data.get("durl") or []
                                alt_urls = [item.get("url") for item in durl if item.get("url")]
                                if alt_urls:
                                    logger.info("成功获取带音频的单文件视频")
                                    # 直接下载这个单文件视频
                                    complete_temp = os.path.join(tmp_dir, f"{safe_title}_complete.mp4")
                                    req = BilibiliParser._build_request(alt_urls[0], headers)
                                    with urllib.request.urlopen(req, timeout=60) as resp:
                                        with open(complete_temp, "wb") as f:
                                            while True:
                                                chunk = resp.read(1024 * 256)
                                                if not chunk:
                                                    break
                                                f.write(chunk)
                                    logger.info(f"带音频的完整视频下载完成: {complete_temp}")
                                    return complete_temp
                        except Exception as e:
                            logger.warning(f"获取带音频的单文件视频失败: {str(e)}")
                    
                    # 如果有音频URL，下载音频流
                    if len(urls) >= 2:
                        req = BilibiliParser._build_request(urls[1], headers)
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            with open(audio_temp, "wb") as f:
                                while True:
                                    chunk = resp.read(1024 * 256)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                        logger.info(f"音频流下载完成: {audio_temp}")
                    else:
                        logger.warning("没有音频流URL可用")
                        audio_temp = None
                    
                    # 尝试使用FFmpeg合并
                    try:
                        import subprocess
                        import shutil
                        
                        # 检查FFmpeg/MP4Box 是否存在
                        possible_paths = [
                            os.path.join(os.environ.get('ProgramFiles', r'C:\\Program Files'), 'ffmpeg', 'bin', 'ffmpeg.exe'),
                            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\\Program Files (x86)'), 'ffmpeg', 'bin', 'ffmpeg.exe'),
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffmpeg.exe'),
                            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ffmpeg.exe'),
                            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ffmpeg.exe'),
                        ]
                        ffmpeg_path = 'ffmpeg'
                        if shutil.which('ffmpeg') is None:
                            for path in possible_paths:
                                if os.path.exists(path):
                                    ffmpeg_path = path
                                    logger.info(f"在路径 {path} 找到FFmpeg")
                                    break

                        # 尝试使用MP4Box合并（替代方案）
                        mp4box_path = 'MP4Box'
                        mp4box_available = shutil.which('MP4Box') is not None or any(
                            os.path.exists(p.replace('ffmpeg.exe', 'MP4Box.exe')) for p in possible_paths
                        )
                        
                                                    # 尝试使用FFmpeg
                        if shutil.which('ffmpeg') is not None or os.path.exists(ffmpeg_path):
                            # 首先检查视频文件格式
                            logger.info("检查视频和音频文件格式")
                            
                            # 检查视频文件
                            probe_cmd = [ffmpeg_path, '-v', 'error', '-show_entries', 'format=format_name', '-of', 'default=noprint_wrappers=1:nokey=1', video_temp]
                            try:
                                video_format = subprocess.run(probe_cmd, capture_output=True, text=False).stdout.decode('utf-8', errors='replace').strip()
                                logger.info(f"视频文件格式: {video_format}")
                            except Exception as e:
                                logger.warning(f"无法检查视频格式: {str(e)}")
                                video_format = "unknown"
                                
                            # 如果有音频文件，检查其格式
                            audio_format = "none"
                            if audio_temp and os.path.exists(audio_temp):
                                probe_cmd = [ffmpeg_path, '-v', 'error', '-show_entries', 'format=format_name', '-of', 'default=noprint_wrappers=1:nokey=1', audio_temp]
                                try:
                                    audio_format = subprocess.run(probe_cmd, capture_output=True, text=False).stdout.decode('utf-8', errors='replace').strip()
                                    logger.info(f"音频文件格式: {audio_format}")
                                except Exception as e:
                                    logger.warning(f"无法检查音频格式: {str(e)}")
                            
                            # 根据文件格式决定处理方式
                            if 'm4s' in video_format.lower() or video_temp.lower().endswith('.m4s'):
                                logger.info("检测到m4s格式，使用特殊处理")
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
                            
                            logger.info(f"执行FFmpeg命令: {' '.join(ffmpeg_cmd)}")
                            
                            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=False)
                            
                            if result.returncode == 0:
                                logger.info("使用FFmpeg合并视频和音频成功")
                                # 检查生成的文件是否包含音频流
                                try:
                                    probe_cmd = [ffmpeg_path, '-v', 'error', '-select_streams', 'a', '-show_streams', '-of', 'default=noprint_wrappers=1:nokey=1', temp_path]
                                    has_audio = len(subprocess.run(probe_cmd, capture_output=True, text=False).stdout) > 0
                                    if has_audio:
                                        logger.info("生成的文件包含音频流")
                                    else:
                                        logger.warning("生成的文件不包含音频流，将尝试其他方法")
                                except Exception as e:
                                    logger.warning(f"无法检查生成文件的音频流: {str(e)}")
                                
                                # 删除临时文件
                                try:
                                    if os.path.exists(video_temp):
                                        os.remove(video_temp)
                                    if audio_temp and os.path.exists(audio_temp):
                                        os.remove(audio_temp)
                                except Exception as e:
                                    logger.warning(f"删除临时文件失败: {str(e)}")
                                    
                                return temp_path
                            else:
                                stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
                                logger.warning(f"FFmpeg合并失败: {stderr_text}")
                        elif mp4box_available:
                            # 尝试使用MP4Box合并
                            # 先将m4s重命名为mp4
                            video_mp4 = video_temp.replace('.m4s', '.mp4')
                            audio_mp4 = audio_temp.replace('.m4s', '.mp4')
                            os.rename(video_temp, video_mp4)
                            os.rename(audio_temp, audio_mp4)
                            
                            mp4box_cmd = [mp4box_path, '-add', video_mp4, '-add', audio_mp4, '-new', temp_path]
                            logger.info(f"执行MP4Box命令: {' '.join(mp4box_cmd)}")
                            
                            result = subprocess.run(mp4box_cmd, capture_output=True, text=False)
                            
                            if result.returncode == 0:
                                logger.info("使用MP4Box合并视频和音频成功")
                                # 删除临时文件
                                try:
                                    os.remove(video_mp4)
                                    os.remove(audio_mp4)
                                except Exception:
                                    pass
                                return temp_path
                            else:
                                stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
                                logger.warning(f"MP4Box合并失败: {stderr_text}")
                        else:
                            logger.warning("未找到FFmpeg或MP4Box，无法合并视频和音频")
                            
                            # 尝试使用Python内置方法合并
                            try:
                                logger.info("尝试使用简单的文件连接方法合并")
                                # 创建一个简单的容器文件
                                with open(temp_path, 'wb') as outfile:
                                    # 写入一个简单的MP4头
                                    outfile.write(b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42mp41\x00\x00\x00\x00')
                                    
                                    # 写入视频数据
                                    with open(video_temp, 'rb') as video_file:
                                        outfile.write(video_file.read())
                                    
                                    # 写入音频数据
                                    with open(audio_temp, 'rb') as audio_file:
                                        outfile.write(audio_file.read())
                                
                                logger.info("简单合并完成，但可能不能正常播放")
                                return temp_path
                            except Exception as e:
                                logger.warning(f"简单合并失败: {str(e)}")
                    except Exception as e:
                        logger.warning(f"合并失败: {str(e)}")
                    
                    # 如果所有方法都失败，返回视频流文件
                    logger.info("将仅使用视频流文件")
                    return video_temp
                
                # 如果不是分离流或合并失败，下载第一个链接
                first_url = urls[0]
                # 推断扩展名
                lower = first_url.lower()
                ext = ".flv" if ".flv" in lower else ".mp4" if ".mp4" in lower else ".ts" if ".ts" in lower else ".dat"
                temp_path = os.path.join(tmp_dir, f"{safe_title}{ext}")
                
                logger.info(f"下载单文件视频: {first_url[:100]}...")
                req = BilibiliParser._build_request(first_url, headers)
                with urllib.request.urlopen(req, timeout=60) as resp:  # nosec - trusted public media URL
                    with open(temp_path, "wb") as f:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)
                
                logger.info(f"单文件视频下载完成: {temp_path}")
                
                # 检查下载的文件是否包含音频流
                try:
                    import subprocess
                    import shutil
                    
                    # 检查是否有FFmpeg
                    ffmpeg_path = 'ffmpeg'
                    if shutil.which('ffmpeg') is None:
                        # 尝试在常见路径查找
                        possible_paths = [
                            os.path.join(os.environ.get('ProgramFiles', 'C:\Program Files'), 'ffmpeg', 'bin', 'ffmpeg.exe'),
                            os.path.join(os.environ.get('ProgramFiles(x86)', 'C:\Program Files (x86)'), 'ffmpeg', 'bin', 'ffmpeg.exe'),
                        ]
                        
                        for path in possible_paths:
                            if os.path.exists(path):
                                ffmpeg_path = path
                                break
                    
                    # 使用FFmpeg检查音频流
                    has_audio = False
                    if shutil.which('ffmpeg') is not None or os.path.exists(ffmpeg_path):
                        # 使用更简单的命令检查音频流
                        probe_cmd = [ffmpeg_path, '-v', 'quiet', '-show_streams', '-select_streams', 'a', temp_path]
                        try:
                            result = subprocess.run(probe_cmd, capture_output=True, text=False, timeout=30)
                            if result.returncode == 0 and len(result.stdout) > 0:
                                has_audio = True
                                logger.info("下载的单文件视频包含音频流")
                            else:
                                logger.warning("下载的单文件视频不包含音频流")
                        except subprocess.TimeoutExpired:
                            logger.warning("FFmpeg检查超时")
                        except Exception as e:
                            logger.warning(f"FFmpeg检查失败: {str(e)}")
                    else:
                        logger.warning("未找到FFmpeg，无法检查音频流，假设有音频")
                        has_audio = True  # 如果没有FFmpeg，假设有音频
                    
                    # 如果没有音频流，尝试其他方法
                    if not has_audio:
                        logger.warning("下载的单文件视频不包含音频流，将尝试重新获取dash格式")
                            
                        # 如果单文件没有音频，尝试重新获取dash格式
                        try:
                            # 重新请求dash格式
                            dash_urls, dash_status = BilibiliParser.get_play_urls_force_dash(info.aid, info.cid)
                            if dash_urls and len(dash_urls) >= 2:
                                logger.info(f"重新获取到dash格式，共{len(dash_urls)}个流")
                                
                                # 下载视频和音频流
                                video_temp = os.path.join(tmp_dir, f"{safe_title}_video.m4s")
                                audio_temp = os.path.join(tmp_dir, f"{safe_title}_audio.m4s")
                                
                                # 下载视频流
                                req = BilibiliParser._build_request(dash_urls[0], headers)
                                with urllib.request.urlopen(req, timeout=60) as resp:
                                    with open(video_temp, "wb") as f:
                                        while True:
                                            chunk = resp.read(1024 * 256)
                                            if not chunk:
                                                break
                                            f.write(chunk)
                                
                                # 下载音频流
                                req = BilibiliParser._build_request(dash_urls[1], headers)
                                with urllib.request.urlopen(req, timeout=60) as resp:
                                    with open(audio_temp, "wb") as f:
                                        while True:
                                            chunk = resp.read(1024 * 256)
                                            if not chunk:
                                                break
                                            f.write(chunk)
                                
                                # 使用FFmpeg合并
                                merged_path = os.path.join(tmp_dir, f"{safe_title}_merged.mp4")
                                merge_cmd = [
                                    ffmpeg_path, 
                                    '-i', video_temp, 
                                    '-i', audio_temp, 
                                    '-c:v', 'copy', 
                                    '-c:a', 'aac',
                                    '-b:a', '192k',
                                    '-y', merged_path
                                ]
                                
                                logger.info(f"合并dash流: {' '.join(merge_cmd)}")
                                merge_result = subprocess.run(merge_cmd, capture_output=True, text=False)
                                
                                if merge_result.returncode == 0:
                                    logger.info("成功合并dash格式的视频和音频")
                                    # 删除临时文件
                                    try:
                                        os.remove(video_temp)
                                        os.remove(audio_temp)
                                        os.remove(temp_path)  # 删除原来的无音频文件
                                    except Exception:
                                        pass
                                    return merged_path
                                else:
                                    stderr_text = merge_result.stderr.decode('utf-8', errors='replace') if merge_result.stderr else ''
                                    logger.warning(f"dash格式合并失败: {stderr_text}")
                            else:
                                logger.warning(f"dash格式获取失败: {dash_status}")
                                # 如果也无法获取dash格式，尝试使用FFmpeg修复原文件
                                if shutil.which('ffmpeg') is not None or os.path.exists(ffmpeg_path):
                                    logger.info("尝试使用FFmpeg修复原文件的音频问题")
                                    fixed_path = os.path.join(tmp_dir, f"{safe_title}_fixed.mp4")
                                    fix_cmd = [
                                        ffmpeg_path,
                                        '-i', temp_path,
                                        '-c:v', 'copy',
                                        '-c:a', 'aac',
                                        '-b:a', '128k',
                                        '-ar', '44100',
                                        '-ac', '2',
                                        '-y', fixed_path
                                    ]
                                    
                                    logger.info(f"修复音频命令: {' '.join(fix_cmd)}")
                                    fix_result = subprocess.run(fix_cmd, capture_output=True, text=False)
                                    
                                    if fix_result.returncode == 0:
                                        logger.info("使用FFmpeg修复音频成功")
                                        try:
                                            os.remove(temp_path)
                                        except Exception:
                                            pass
                                        return fixed_path
                                    else:
                                        stderr_text = fix_result.stderr.decode('utf-8', errors='replace') if fix_result.stderr else ''
                                        logger.warning(f"FFmpeg修复失败: {stderr_text}")
                        except Exception as e:
                            logger.warning(f"重新获取dash格式失败: {str(e)}")
                    else:
                        logger.info("单文件视频包含音频流，但为了确保兼容性，将重新编码")
                        # 即使检测到有音频，也重新编码以确保兼容性
                        if shutil.which('ffmpeg') is not None or os.path.exists(ffmpeg_path):
                            fixed_path = os.path.join(tmp_dir, f"{safe_title}_fixed.mp4")
                            fix_cmd = [
                                ffmpeg_path,
                                '-i', temp_path,
                                '-c:v', 'libx264',  # 重新编码视频以确保兼容性
                                '-c:a', 'aac',      # 重新编码音频以确保兼容性
                                '-b:v', '1000k',    # 设置视频比特率
                                '-b:a', '128k',     # 设置音频比特率
                                '-ar', '44100',     # 设置音频采样率
                                '-ac', '2',         # 设置双声道
                                '-preset', 'fast',  # 使用快速编码预设
                                '-y', fixed_path
                            ]
                            
                            logger.info(f"重新编码视频命令: {' '.join(fix_cmd)}")
                            fix_result = subprocess.run(fix_cmd, capture_output=True, text=False)
                            
                            if fix_result.returncode == 0:
                                logger.info("重新编码视频成功")
                                # 验证重新编码的文件是否有音频
                                verify_cmd = [ffmpeg_path, '-v', 'quiet', '-show_streams', '-select_streams', 'a', fixed_path]
                                verify_result = subprocess.run(verify_cmd, capture_output=True, text=False)
                                
                                if len(verify_result.stdout) > 0:
                                    logger.info("重新编码的文件包含音频流")
                                    try:
                                        os.remove(temp_path)
                                    except Exception:
                                        pass
                                    return fixed_path
                                else:
                                    logger.warning("重新编码的文件仍然没有音频流")
                            else:
                                stderr_text = fix_result.stderr.decode('utf-8', errors='replace') if fix_result.stderr else ''
                                logger.warning(f"重新编码失败: {stderr_text}")
                        
                        # 如果重新编码失败或没有FFmpeg，直接返回原文件
                        logger.info("使用原始下载的文件")
                except Exception as e:
                    logger.warning(f"检查音频流失败: {str(e)}")
                
                return temp_path
            except Exception as e:
                from src.common.logger import get_logger
                logger = get_logger("bilibili_handler")
                logger.error(f"下载视频失败: {e}")
                return None

        temp_path = await asyncio.get_running_loop().run_in_executor(None, _download_to_temp)
        if not temp_path:
            return True, True, "已发送直链，下载视频失败"

        caption = f"{info.title}"

        async def _try_send(path: str) -> bool:
            # 使用 send_api 发送视频或文件
            try:
                # 尝试发送视频
                if await send_api.custom_to_stream("video", path, stream_id, display_message=caption):
                    return True
            except Exception:
                pass

            try:
                # 尝试发送文件
                if await send_api.custom_to_stream("file", path, stream_id, display_message=os.path.basename(path)):
                    return True
            except Exception:
                pass

            return False

        sent_ok = await _try_send(temp_path)
        if not sent_ok:
            await self._send_text("直链已发送，但宿主暂不支持直接发送视频文件。", stream_id)
        return True, True, "已发送直链与视频（若宿主支持）"


class BilibiliNoopAction(BaseAction):
    """占位Action，避免仅事件处理器导致的加载器不识别问题。"""

    # 使用几乎不可能触发的关键字，确保不会影响实际行为
    focus_activation_type = ActionActivationType.KEYWORD
    normal_activation_type = ActionActivationType.KEYWORD

    action_name = "bilibili_video_sender_noop"
    action_description = "占位，不会实际触发"
    activation_keywords = ["__never_triggers__"]
    keyword_case_sensitive = False
    action_parameters = {}
    action_require = ["仅用于让插件被识别，不会被调用"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        return False, "noop"


@register_plugin
class BilibiliVideoSenderPlugin(BasePlugin):
    """B站视频直链解析与自动发送插件。"""

    plugin_name: str = "bilibili_video_sender_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "name": ConfigField(type=str, default="bilibili_video_sender_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (BilibiliNoopAction.get_action_info(), BilibiliNoopAction),
            (BilibiliAutoSendHandler.get_handler_info(), BilibiliAutoSendHandler),
        ]


