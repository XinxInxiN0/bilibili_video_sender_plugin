# -*- coding: utf-8 -*-
"""哔哩哔哩视频链接解析与 WBI 签名。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .auth import apply_set_cookie, build_cookie_header, has_login_cookie, normalize_credentials
from .ffmpeg import ffmpeg_manager

_logger = logging.getLogger("plugin.bilibili_video_sender.parser")


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

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/144.0.0.0 Safari/537.36"
    )

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
        16: "360P 流畅",
        32: "480P 清晰",
        64: "720P 高清",
        74: "720P60 高帧率",
        80: "1080P 高清",
        112: "1080P+ 高码率",
        116: "1080P60 高帧率",
        120: "4K 超清",
        125: "HDR 真彩色",
        126: "杜比视界",
        127: "8K 超高清",
    }

    @staticmethod
    def safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """清理 URL 尾部可能出现的标点符号。"""
        return url.rstrip(").,，。!?》】】〕」\"'") if url else url

    @staticmethod
    def extract_qn_from_text(text: str) -> Optional[int]:
        """从原始文本中解析 qn 参数，作为 URL 提取丢参时的兜底。"""
        if not text:
            return None
        match = BilibiliParser.QN_TEXT_PATTERN.search(text)
        if not match:
            return None
        return BilibiliParser.safe_int(match.group(1), 0) or None

    @staticmethod
    def extract_page_param(url: str) -> int:
        """解析分P参数 p，默认为 1。"""
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query or "")
            p_raw = qs.get("p", [None])[0]
            p_val = BilibiliParser.safe_int(p_raw, 1)
            return p_val if p_val > 0 else 1
        except Exception:
            return 1

    @staticmethod
    def extract_qn_param(url: str) -> Optional[int]:
        """解析清晰度参数 qn，返回 None 表示未指定。"""
        if not url:
            return None
        try:
            parsed = urllib.parse.urlparse(BilibiliParser._sanitize_url(url))
            qs = urllib.parse.parse_qs(parsed.query or "")
            qn_raw = qs.get("qn", [None])[0]
            if qn_raw is None:
                return None
            qn_val = BilibiliParser.safe_int(qn_raw, 0)
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
        normalized: List[str] = []
        for u in urls:
            u2 = u.replace("http:", "https:")
            if u2 not in normalized:
                normalized.append(u2)
        return normalized

    @staticmethod
    def get_qn_name(qn: int) -> str:
        return BilibiliParser.QN_INFO.get(qn, f"未知({qn})")

    @staticmethod
    def validate_config(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """验证配置参数的有效性"""
        opts = options or {}
        validation_result: Dict[str, Any] = {"valid": True, "warnings": [], "errors": [], "recommendations": []}

        # 检查Cookie配置
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()

        if not sessdata:
            validation_result["warnings"].append("未配置SESSDATA，将使用游客模式")
            validation_result["recommendations"].append("建议配置 auth.json 以获得更好的清晰度和自动续期能力")
        elif len(sessdata) < 10:
            validation_result["errors"].append("SESSDATA长度异常，可能配置错误")
            validation_result["valid"] = False
        else:
            # 尝试解析 SESSDATA 内嵌的过期时间戳（URL 解码后格式: value,timestamp,suffix）
            try:
                decoded = urllib.parse.unquote(sessdata)
                parts = decoded.split(",")
                if len(parts) >= 2:
                    expiry_ts = int(parts[1])
                    if 0 < expiry_ts < time.time():
                        expire_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry_ts))
                        warn_msg = f"SESSDATA 已过期（过期时间: {expire_dt}），B站将退回游客模式，清晰度受限，请更新 auth.json"
                        validation_result["warnings"].append(warn_msg)
                        _logger.warning(warn_msg)
            except Exception:
                pass

        if not buvid3:
            # buvid3 为可选项，仅影响 session 参数生成
            validation_result["recommendations"].append("未配置Buvid3（可选），如需生成session参数可补充")
        elif len(buvid3) < 10:
            validation_result["warnings"].append("Buvid3长度异常，可能配置错误（非必填）")

        # 检查清晰度配置
        requested_qn = BilibiliParser.safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0
        if requested_qn == 0:
            effective_qn = 64 if sessdata else 32
            qn_name = BilibiliParser.get_qn_name(effective_qn)
            _logger.info("清晰度配置: 自动(%s) (qn=0)", qn_name)
        else:
            effective_qn = requested_qn
            qn_name = BilibiliParser.get_qn_name(requested_qn)
            if requested_qn not in BilibiliParser.QN_INFO:
                validation_result["warnings"].append(f"qn={requested_qn} 不在常见清晰度列表，可能无效")
            _logger.info("清晰度配置: %s (qn=%d, strict=%s)", qn_name, requested_qn, strict_qn)

        if effective_qn >= 64 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}清晰度但未配置Cookie，可能失败")
        if effective_qn >= 80 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}清晰度需要大会员账号")
        if effective_qn >= 116 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}高帧率需要大会员账号")
        if effective_qn >= 125 and not sessdata:
            validation_result["warnings"].append(f"请求{qn_name}需要大会员账号")

        # 记录验证结果
        if validation_result["warnings"]:
            _logger.debug("Config warnings: %s", validation_result["warnings"])
        if validation_result["errors"]:
            _logger.error("Config errors: %s", validation_result["errors"])
        if validation_result["recommendations"]:
            _logger.debug("Config suggestions: %s", validation_result["recommendations"])
        _logger.debug("Config validation: %s", "pass" if validation_result["valid"] else "fail")
        return validation_result

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

        if target_qn > 0:
            if strict_qn:
                eligible = [v for v in videos if BilibiliParser.safe_int(v.get("id")) == target_qn]
                if not eligible:
                    return None, None, "strict_no_match"
            else:
                eligible = [v for v in videos if BilibiliParser.safe_int(v.get("id")) <= target_qn]
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
            best_id = max((BilibiliParser.safe_int(v.get("id")) for v in eligible), default=0)
            if best_id > 0:
                candidates = [v for v in eligible if BilibiliParser.safe_int(v.get("id")) == best_id]
            else:
                candidates = list(eligible)

        candidates.sort(
            key=lambda v: (
                BilibiliParser._codec_rank(str(v.get("codecs", ""))),
                -BilibiliParser.safe_int(v.get("bandwidth")),
            )
        )
        best_video = candidates[0] if candidates else None
        selected_qn = BilibiliParser.safe_int(best_video.get("id")) if best_video else None
        return best_video, selected_qn, "fallback" if fallback else "ok"

    @staticmethod
    def _extract_refreshed_sessdata(response_headers: Any, current_sessdata: str) -> Optional[str]:
        """从响应 Set-Cookie 中提取 B站刷新的 SESSDATA（与当前值不同时才返回）。"""
        try:
            cookies = response_headers.get_all("Set-Cookie") or []
            for cookie in cookies:
                for part in cookie.split(";"):
                    part = part.strip()
                    if part.upper().startswith("SESSDATA="):
                        value = part[len("SESSDATA="):].strip()
                        if value and value.lower() != "deleted" and value != current_sessdata:
                            return value
        except Exception:
            pass
        return None

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
    def _credentials_from_options(options: Dict[str, Any]) -> Dict[str, Any]:
        credentials = normalize_credentials(options.get("credentials") if isinstance(options.get("credentials"), dict) else {})
        if options.get("sessdata") and not credentials.get("SESSDATA"):
            credentials["SESSDATA"] = str(options.get("sessdata", "")).strip()
        if options.get("buvid3") and not credentials.get("buvid3"):
            credentials["buvid3"] = str(options.get("buvid3", "")).strip()
        return normalize_credentials(credentials)

    @staticmethod
    def _cookie_header_from_options(options: Dict[str, Any]) -> str:
        cookie_header = str(options.get("cookie_header", "") or "").strip()
        if cookie_header:
            return cookie_header
        return build_cookie_header(BilibiliParser._credentials_from_options(options))

    @staticmethod
    def _fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发送 HTTP 请求并解析 JSON。"""
        req = BilibiliParser._build_request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data = resp.read()
        return json.loads(data.decode("utf-8", errors="ignore"))

    @staticmethod
    def _follow_redirect(url: str) -> str:
        """跟踪短链接跳转。"""
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
        return None

    @staticmethod
    def find_first_bilibili_url(text: str) -> Optional[str]:
        """从文本中提取第一个 B站视频链接。"""
        short = BilibiliParser.B23_SHORT_PATTERN.search(text)
        if short:
            return BilibiliParser._sanitize_url(short.group(0))

        match = BilibiliParser.VIDEO_URL_PATTERN.search(text)
        if match:
            return BilibiliParser._sanitize_url(match.group(0))
        return None

    @staticmethod
    def get_view_info_by_url(
        url: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Optional[BilibiliVideoInfo]:
        """通过视频 URL 获取视频信息。"""
        bvid = BilibiliParser._extract_bvid(url)
        page_index = BilibiliParser.extract_page_param(url)

        opts = options or {}
        credentials = BilibiliParser._credentials_from_options(opts)
        cookie_header = BilibiliParser._cookie_header_from_options(opts)
        headers: Dict[str, str] = {}
        if cookie_header:
            headers["Cookie"] = cookie_header

        if bvid:
            query = f"bvid={urllib.parse.quote(bvid)}"
        else:
            m = re.search(r"/video/av(?P<aid>\d+)", url)
            if not m:
                return None
            query = f"aid={m.group('aid')}"

        api = f"https://api.bilibili.com/x/web-interface/view?{query}"
        payload = BilibiliParser._fetch_json(api, headers=headers)
        if payload.get("code") != 0:
            return None

        data = payload.get("data", {})
        pages = data.get("pages") or []
        if not pages:
            return None

        if page_index > len(pages):
            _logger.warning("分P参数超出范围: p=%d, total=%d，回退到 P1", page_index, len(pages))
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
        """获取视频播放地址（DASH 或 durl 格式）。"""
        opts = options or {}

        credentials = BilibiliParser._credentials_from_options(opts)
        sessdata = str(credentials.get("SESSDATA", "")).strip()
        buvid3 = str(credentials.get("buvid3", "")).strip()
        cookie_header = BilibiliParser._cookie_header_from_options(opts)
        requested_qn = BilibiliParser.safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0

        has_cookie = has_login_cookie(credentials)

        if not has_cookie:
            _logger.warning("未提供 Cookie，将使用游客模式（清晰度限制）")

        if requested_qn == 0:
            qn = 64 if has_cookie else 32
            _logger.debug("Auto quality enabled: effective_qn=%d", qn)
        else:
            qn = requested_qn

        fourk = 1 if qn >= 120 else 0

        qn_name = BilibiliParser.get_qn_name(qn)
        if qn >= 64 and not has_cookie:
            _logger.warning("请求 %s 清晰度但未登录，可能失败", qn_name)
        if qn >= 80 and not has_cookie:
            _logger.warning("请求 %s 清晰度需要大会员账号", qn_name)
        if qn >= 116 and not has_cookie:
            _logger.warning("请求 %s 高帧率需要大会员账号", qn_name)
        if qn >= 125 and not has_cookie:
            _logger.warning("请求 %s 需要大会员账号", qn_name)

        opts["requested_qn"] = requested_qn
        opts["effective_qn"] = qn
        opts["qn_strict"] = strict_qn

        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fnver": "0",
            "fnval": "4048",
            "fourk": str(fourk),
            "platform": "pc",
        }

        if qn > 0:
            params["qn"] = str(qn)

        if buvid3:
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash

        if not has_cookie:
            params["gaia_source"] = "view-card"

        api_base = "https://api.bilibili.com/x/player/wbi/playurl"

        try:
            final_params = BilibiliWbiSigner.sign_params(params)
        except Exception as e:
            _logger.warning("WBI 签名失败，降级到非 WBI 接口: %s", e)
            api_base = "https://api.bilibili.com/x/player/playurl"
            final_params = params

        query_str = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query_str}"

        headers: Dict[str, str] = {}
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
                # 捕获 B站可能刷新的 Cookie（rolling session）
                updated_credentials = apply_set_cookie(credentials, resp.headers)
                if updated_credentials != credentials:
                    opts["credentials"] = updated_credentials
                    opts["auth_refreshed"] = True
                    _logger.info("B站 Cookie 已由响应头自动刷新")
        except Exception as e:
            _logger.error("HTTP请求失败: %s", e)
            return None, f"网络请求失败: {e}"

        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            _logger.error("JSON解析失败: %s", e)
            return None, "响应数据格式错误"

        if payload.get("code") != 0:
            error_msg = payload.get("message", "接口返回错误")
            _logger.error("API返回错误: code=%s, message=%s", payload.get("code"), error_msg)
            return None, error_msg

        _logger.debug("API请求成功，开始解析响应数据")
        data = payload.get("data", {})

        dash = data.get("dash")
        if not dash:
            durl = data.get("durl") or []
            if durl:
                _logger.debug("找到 durl 格式数据，共 %d 个文件", len(durl))
                if len(durl) > 1:
                    _logger.warning("durl 为多段视频（%d 段），当前仅处理第一段", len(durl))
                item = durl[0]
                primary = item.get("url") or item.get("baseUrl") or item.get("base_url")
                backups = item.get("backup_url") or item.get("backupUrl") or []
                urls = BilibiliParser._normalize_stream_urls(primary, backups)
                if urls:
                    return {"type": "durl", "urls": urls}, "ok (durl格式)"
            return None, "未找到 dash 数据"

        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        _logger.debug("找到 %d 个视频流和 %d 个音频流", len(videos), len(audios))

        # 处理杜比和 flac 音频
        dolby_audios = []
        flac_audios = []

        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])

        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]

        all_audios = audios + dolby_audios + flac_audios

        if not videos:
            _logger.warning("未找到视频流")
            return None, "未找到视频流"

        if not all_audios:
            _logger.warning("未找到音频流")

        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)

        best_video, selected_qn, selection_status = BilibiliParser._select_video_stream(videos, qn, strict_qn)
        if not best_video:
            if selection_status == "strict_no_match":
                requested_name = BilibiliParser.get_qn_name(requested_qn)
                return None, f"请求清晰度不可用: {requested_name}"
            _logger.error("Failed to select video stream")
            return None, "未获取到播放地址"

        if selected_qn is not None:
            selected_name = BilibiliParser.get_qn_name(selected_qn)
            opts["selected_qn"] = selected_qn
            opts["selected_qn_name"] = selected_name
            if requested_qn:
                opts["requested_qn_name"] = BilibiliParser.get_qn_name(requested_qn)
            else:
                opts["requested_qn_name"] = "自动"

            if requested_qn != 0 and selected_qn != requested_qn:
                _logger.info(
                    "Quality downgrade: requested %s (qn=%d), selected %s (qn=%d)",
                    BilibiliParser.get_qn_name(requested_qn),
                    requested_qn,
                    selected_name,
                    selected_qn,
                )
            elif requested_qn == 0 and selected_qn != qn:
                # 自动模式：effective target 是 qn，但实际只拿到 selected_qn（低于预期）
                # 通常因为 SESSDATA 过期或权限不足，B站退回游客级别的流
                _logger.warning(
                    "自动清晰度降级: 期望 %s (qn=%d)，实际获得 %s (qn=%d)，"
                    "B站登录凭据可能已过期或账号权限不足，请更新 auth.json",
                    BilibiliParser.get_qn_name(qn),
                    qn,
                    selected_name,
                    selected_qn,
                )
            else:
                _logger.info("Quality selected: requested_qn=%d, selected_qn=%d", requested_qn, selected_qn)

        if selection_status == "fallback":
            _logger.info("No eligible streams for qn=%d, fell back to best available stream", qn)

        video_url = best_video.get("baseUrl") or best_video.get("base_url")
        video_backups = best_video.get("backupUrl") or best_video.get("backup_url") or []
        video_urls = BilibiliParser._normalize_stream_urls(video_url, video_backups)

        audio_urls: List[str] = []
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            audio_backups = best_audio.get("backupUrl") or best_audio.get("backup_url") or []
            audio_urls = BilibiliParser._normalize_stream_urls(audio_url, audio_backups)

        if video_urls:
            return {"type": "dash", "video_urls": video_urls, "audio_urls": audio_urls}, "ok"

        _logger.error("Failed to get playback URLs")
        return None, "未获取到播放地址"

    @staticmethod
    def get_play_urls_force_dash(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """强制获取 DASH 格式的视频和音频流。"""
        opts = options or {}

        _logger.debug("=== Force fetch DASH format ===")

        credentials = BilibiliParser._credentials_from_options(opts)
        sessdata = str(credentials.get("SESSDATA", "")).strip()
        buvid3 = str(credentials.get("buvid3", "")).strip()
        cookie_header = BilibiliParser._cookie_header_from_options(opts)
        requested_qn = BilibiliParser.safe_int(opts.get("qn", 0))
        strict_qn = bool(opts.get("qn_strict", False)) and requested_qn != 0

        has_cookie = has_login_cookie(credentials)

        if not has_cookie:
            _logger.warning("Force DASH: no Cookie, may affect HD fetching")

        if requested_qn == 0:
            qn = 64 if has_cookie else 32
        else:
            qn = requested_qn

        fourk = 1 if qn >= 120 else 0

        qn_name = BilibiliParser.get_qn_name(qn)
        if qn >= 64 and not has_cookie:
            _logger.warning("Force DASH: 请求 %s 清晰度但未登录，可能失败", qn_name)
        if qn >= 80 and not has_cookie:
            _logger.warning("Force DASH: 请求 %s 清晰度需要大会员账号", qn_name)
        if qn >= 116 and not has_cookie:
            _logger.warning("Force DASH: 请求 %s 高帧率需要大会员账号", qn_name)
        if qn >= 125 and not has_cookie:
            _logger.warning("Force DASH: 请求 %s 需要大会员账号", qn_name)

        opts["requested_qn"] = requested_qn
        opts["effective_qn"] = qn
        opts["qn_strict"] = strict_qn

        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fourk": str(fourk),
            "fnver": "0",
            "fnval": "4048",
            "platform": "pc",
        }

        if qn > 0:
            params["qn"] = str(qn)

        if buvid3:
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash

        if not has_cookie:
            params["gaia_source"] = "view-card"

        api_base = "https://api.bilibili.com/x/player/wbi/playurl"

        try:
            final_params = BilibiliWbiSigner.sign_params(params)
        except Exception as e:
            _logger.warning("Force DASH: WBI签名失败，降级到非WBI接口: %s", e)
            api_base = "https://api.bilibili.com/x/player/playurl"
            final_params = params

        query_str = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query_str}"

        headers: Dict[str, str] = {}
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
                # 捕获 B站可能刷新的 Cookie（rolling session）
                updated_credentials = apply_set_cookie(credentials, resp.headers)
                if updated_credentials != credentials:
                    opts["credentials"] = updated_credentials
                    opts["auth_refreshed"] = True
                    _logger.info("Force DASH: B站 Cookie 已由响应头自动刷新")
        except Exception as e:
            _logger.error("Force DASH HTTP error: %s", e)
            return None, f"Force DASH network error: {e}"

        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            _logger.error("Force DASH JSON parse error: %s", e)
            return None, "Force DASH response format error"

        if payload.get("code") != 0:
            error_msg = payload.get("message", "API error")
            _logger.error("Force DASH API error: code=%s, msg=%s", payload.get("code"), error_msg)
            return None, error_msg

        _logger.debug("Force DASH request successful, parsing response")
        data = payload.get("data", {})

        durl = data.get("durl") or []
        if durl:
            _logger.debug("Force DASH also returned durl format: %d files (single-file only)", len(durl))
            if len(durl) > 1:
                _logger.warning("Force DASH: durl为多段视频（%d段），当前仅处理第一段", len(durl))
            item = durl[0]
            primary = item.get("url") or item.get("baseUrl") or item.get("base_url")
            backups = item.get("backup_url") or item.get("backupUrl") or []
            urls = BilibiliParser._normalize_stream_urls(primary, backups)
            if urls:
                return {"type": "durl", "urls": urls}, "ok (durl格式)"

        dash = data.get("dash")
        if not dash:
            _logger.warning("Force DASH: no dash data found")
            return None, "No dash data"

        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        _logger.debug("Force DASH: %d video streams, %d audio streams", len(videos), len(audios))

        dolby_audios = []
        flac_audios = []

        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])

        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]

        all_audios = audios + dolby_audios + flac_audios

        if not videos or not all_audios:
            _logger.warning("Force DASH: missing streams - video=%d, audio=%d", len(videos), len(all_audios))
            return None, "Missing video or audio streams"

        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)

        best_video, selected_qn, selection_status = BilibiliParser._select_video_stream(videos, qn, strict_qn)
        if not best_video:
            if selection_status == "strict_no_match":
                requested_name = BilibiliParser.get_qn_name(requested_qn)
                return None, f"Force DASH: 请求清晰度不可用: {requested_name}"
            return None, "Force DASH: missing video stream"

        if selected_qn is not None:
            selected_name = BilibiliParser.get_qn_name(selected_qn)
            opts["selected_qn"] = selected_qn
            opts["selected_qn_name"] = selected_name
            if requested_qn:
                opts["requested_qn_name"] = BilibiliParser.get_qn_name(requested_qn)
            else:
                opts["requested_qn_name"] = "自动"

            if requested_qn != 0 and selected_qn != requested_qn:
                _logger.info(
                    "Force DASH quality downgrade: requested %s (qn=%d), selected %s (qn=%d)",
                    BilibiliParser.get_qn_name(requested_qn),
                    requested_qn,
                    selected_name,
                    selected_qn,
                )
            elif requested_qn == 0 and selected_qn != qn:
                # 自动模式：effective target 是 qn，但实际只拿到 selected_qn（低于预期）
                _logger.warning(
                    "Force DASH 自动清晰度降级: 期望 %s (qn=%d)，实际获得 %s (qn=%d)，"
                    "B站登录凭据可能已过期或账号权限不足，请更新 auth.json",
                    BilibiliParser.get_qn_name(qn),
                    qn,
                    selected_name,
                    selected_qn,
                )
            else:
                _logger.info("Force DASH quality selected: requested_qn=%d, selected_qn=%d", requested_qn, selected_qn)

        if selection_status == "fallback":
            _logger.info("Force DASH: no eligible streams for qn=%d, fell back to best available stream", qn)

        video_url = best_video.get("baseUrl") or best_video.get("base_url")
        video_backups = best_video.get("backupUrl") or best_video.get("backup_url") or []
        video_urls = BilibiliParser._normalize_stream_urls(video_url, video_backups)

        audio_urls: List[str] = []
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            audio_backups = best_audio.get("backupUrl") or best_audio.get("backup_url") or []
            audio_urls = BilibiliParser._normalize_stream_urls(audio_url, audio_backups)

        if video_urls:
            return {"type": "dash", "video_urls": video_urls, "audio_urls": audio_urls}, "ok"

        return None, "Force DASH: no usable streams"

    @staticmethod
    def get_video_duration(video_path: str) -> Optional[float]:
        """获取视频时长（秒）。"""
        try:
            ffprobe_path = ffmpeg_manager.get_ffprobe_path()

            if not ffprobe_path:
                _logger.warning("未找到 ffprobe，无法获取视频时长")
                return None

            cmd = [
                ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=False)

            if result.returncode == 0:
                duration_str = result.stdout.decode("utf-8", errors="replace").strip()
                try:
                    duration = float(duration_str)
                    _logger.debug("Video duration: %.1fs", duration)
                    return duration
                except ValueError:
                    _logger.warning("Failed to parse duration: '%s'", duration_str)
                    return None

            _logger.warning("ffprobe failed with code: %d", result.returncode)
            return None
        except Exception as e:
            _logger.error("Error getting video duration: %s", e)
            return None


class BilibiliWbiSigner:
    """WBI 签名工具：自动获取 wbi key 并缓存，生成 w_rid/wts。"""

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
        """从 nav 接口拉取 wbi img/sub key。"""
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
        raw = img_key + sub_key
        if len(raw) < 64:
            _logger.warning("WBI key length insufficient: %d", len(raw))
            raise ValueError("WBI key length insufficient")
        mixed = "".join(raw[i] for i in cls._mixin_key_indices)[:32]
        cls._cached_mixin_key = mixed
        cls._cached_at = now
        return mixed

    @classmethod
    def sign_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """生成 wts 和 w_rid 并返回带签名的参数副本。"""
        mixin_key = cls._gen_mixin_key()
        safe_params: Dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str):
                v2 = re.sub(r"[!'()*]", "", v)
            else:
                v2 = v
            safe_params[k] = v2
        wts = int(time.time())
        safe_params["wts"] = wts
        items = sorted(safe_params.items(), key=lambda x: x[0])
        query = urllib.parse.urlencode(items, doseq=True)
        w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        safe_params["w_rid"] = w_rid
        return safe_params
