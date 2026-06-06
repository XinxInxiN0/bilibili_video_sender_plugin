# -*- coding: utf-8 -*-
"""Bilibili Web credential persistence and cookie refresh helpers."""
from __future__ import annotations

import html
import gzip
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional

_logger = logging.getLogger("plugin.bilibili_video_sender.auth")


COOKIE_FIELD_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
)
AUTH_FIELD_NAMES = COOKIE_FIELD_NAMES + ("ac_time_value", "updated_at")
AUTH_CONFIG_FIELD_NAMES = COOKIE_FIELD_NAMES + ("ac_time_value",)

COOKIE_INFO_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
CORRESPOND_URL = "https://www.bilibili.com/correspond/1/{correspond_path}"
COOKIE_REFRESH_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
COOKIE_CONFIRM_URL = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"

PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


@dataclass
class AuthCheckResult:
    status: str
    message: str = ""
    refresh_required: bool = False
    timestamp_ms: int = 0


@dataclass
class AuthRefreshResult:
    status: str
    credentials: Dict[str, Any]
    message: str = ""
    refreshed: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "not_needed", "refreshed"}


def normalize_credentials(credentials: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source = credentials or {}
    normalized: Dict[str, Any] = {}
    aliases = {
        "sessdata": "SESSDATA",
        "dedeuserid": "DedeUserID",
        "dedeuserid_ckmd5": "DedeUserID__ckMd5",
        "DedeUserID__ckMd5": "DedeUserID__ckMd5",
        "refresh_token": "ac_time_value",
    }
    for key, value in source.items():
        target = aliases.get(str(key), str(key))
        if target in AUTH_FIELD_NAMES and value is not None:
            normalized[target] = str(value).strip() if target != "updated_at" else value
    if normalized and "updated_at" not in normalized:
        normalized["updated_at"] = int(time.time())
    return {k: v for k, v in normalized.items() if v not in ("", None)}


def has_login_cookie(credentials: Optional[Dict[str, Any]]) -> bool:
    return bool((credentials or {}).get("SESSDATA"))


def build_cookie_header(credentials: Optional[Dict[str, Any]]) -> str:
    creds = normalize_credentials(credentials)
    parts = []
    for name in COOKIE_FIELD_NAMES:
        value = str(creds.get(name, "")).strip()
        if value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def save_auth_config(config_path: str, credentials: Dict[str, Any]) -> None:
    """Write Bilibili credentials back to the [auth] section of config.toml."""
    payload = normalize_credentials(credentials)
    auth_lines = ["[auth]\n"]
    for name in AUTH_CONFIG_FIELD_NAMES:
        value = str(payload.get(name, "") or "")
        auth_lines.append(f"{name} = {json.dumps(value, ensure_ascii=False)}\n")

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            original_lines = f.readlines()
    else:
        original_lines = []

    start = None
    end = None
    table_pattern = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")
    auth_pattern = re.compile(r"^\s*\[auth\]\s*(?:#.*)?$")
    for idx, line in enumerate(original_lines):
        if auth_pattern.match(line):
            start = idx
            end = len(original_lines)
            for next_idx in range(idx + 1, len(original_lines)):
                if table_pattern.match(original_lines[next_idx]):
                    end = next_idx
                    break
            break

    if start is None:
        new_lines = list(original_lines)
        if new_lines and new_lines[-1].strip():
            new_lines.append("\n")
        if new_lines:
            new_lines.append("\n")
        new_lines.extend(auth_lines)
    else:
        replacement = list(auth_lines)
        if end < len(original_lines) and (not replacement[-1].endswith("\n") or original_lines[end - 1].strip()):
            replacement.append("\n")
        new_lines = original_lines[:start] + replacement + original_lines[end:]

    tmp_path = f"{config_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.replace(tmp_path, config_path)


def extract_set_cookie_values(response_headers: Any) -> Dict[str, str]:
    cookies: list[str] = []
    try:
        if hasattr(response_headers, "get_all"):
            cookies = response_headers.get_all("Set-Cookie") or []
        elif hasattr(response_headers, "getheaders"):
            cookies = response_headers.getheaders("Set-Cookie") or []
        else:
            single = response_headers.get("Set-Cookie") if response_headers else None
            cookies = [single] if single else []
    except Exception:
        cookies = []

    result: Dict[str, str] = {}
    for cookie_header in cookies:
        try:
            parsed = SimpleCookie()
            parsed.load(cookie_header)
            for name in COOKIE_FIELD_NAMES:
                morsel = parsed.get(name)
                if morsel and morsel.value and morsel.value.lower() != "deleted":
                    result[name] = morsel.value
        except Exception:
            for part in str(cookie_header).split(";"):
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name in COOKIE_FIELD_NAMES and value and value.lower() != "deleted":
                    result[name] = value
    return result


def apply_set_cookie(credentials: Dict[str, Any], response_headers: Any) -> Dict[str, Any]:
    updated = dict(normalize_credentials(credentials))
    cookie_updates = extract_set_cookie_values(response_headers)
    if not cookie_updates:
        return updated
    updated.update(cookie_updates)
    if updated:
        updated["updated_at"] = int(time.time())
    return updated


class BilibiliAuthRefresher:
    """Synchronous Bilibili Web cookie refresh client."""

    @staticmethod
    def cryptography_available() -> bool:
        try:
            from cryptography.hazmat.primitives import hashes  # noqa: F401
            from cryptography.hazmat.primitives.asymmetric import padding  # noqa: F401
            from cryptography.hazmat.primitives.serialization import load_pem_public_key  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def check_refresh(credentials: Dict[str, Any]) -> AuthCheckResult:
        creds = normalize_credentials(credentials)
        if not has_login_cookie(creds):
            return AuthCheckResult(status="guest", message="未配置 B站登录凭据")

        params: Dict[str, str] = {}
        if creds.get("bili_jct"):
            params["csrf"] = str(creds["bili_jct"])
        url = COOKIE_INFO_URL
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Cookie": build_cookie_header(creds)} if build_cookie_header(creds) else {}

        try:
            payload = _fetch_json(url, headers=headers)
        except Exception as e:
            return AuthCheckResult(status="check_failed", message=f"登录态检查失败: {e}")

        if payload.get("code") != 0:
            message = str(payload.get("message") or "账号未登录")
            return AuthCheckResult(status="invalid", message=message)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return AuthCheckResult(status="invalid", message="登录态检查响应缺少 data")
        refresh_required = bool(data.get("refresh"))
        timestamp_ms = int(data.get("timestamp") or int(time.time() * 1000))
        return AuthCheckResult(
            status="refresh_required" if refresh_required else "ok",
            refresh_required=refresh_required,
            timestamp_ms=timestamp_ms,
        )

    @classmethod
    def refresh_if_needed(cls, credentials: Dict[str, Any]) -> AuthRefreshResult:
        creds = normalize_credentials(credentials)
        check = cls.check_refresh(creds)
        if check.status in {"guest", "ok"}:
            return AuthRefreshResult(status="not_needed", credentials=creds, message=check.message)
        if check.status != "refresh_required":
            return AuthRefreshResult(status=check.status, credentials={}, message=check.message)

        missing = [name for name in ("bili_jct", "ac_time_value") if not creds.get(name)]
        if missing:
            return AuthRefreshResult(
                status="missing_material",
                credentials={},
                message=f"缺少刷新凭据: {', '.join(missing)}",
            )
        if not cls.cryptography_available():
            return AuthRefreshResult(
                status="dependency_missing",
                credentials={},
                message="缺少 cryptography，无法主动刷新 B站 Cookie",
            )

        try:
            return cls._refresh(creds, check.timestamp_ms)
        except Exception as e:
            return AuthRefreshResult(status="refresh_failed", credentials={}, message=str(e))

    @classmethod
    def force_refresh(cls, credentials: Dict[str, Any], timestamp_ms: Optional[int] = None) -> AuthRefreshResult:
        """Force a real refresh for controlled diagnostics.

        Unlike refresh_if_needed(), this method may refresh even when /cookie/info
        currently reports refresh=false. It still requires a valid logged-in
        cookie because Bilibili's refresh endpoint is cookie-authenticated.
        """
        creds = normalize_credentials(credentials)
        if not has_login_cookie(creds):
            return AuthRefreshResult(status="guest", credentials={}, message="未配置 B站登录凭据")

        missing = [name for name in ("bili_jct", "ac_time_value") if not creds.get(name)]
        if missing:
            return AuthRefreshResult(
                status="missing_material",
                credentials={},
                message=f"缺少刷新凭据: {', '.join(missing)}",
            )
        if not cls.cryptography_available():
            return AuthRefreshResult(
                status="dependency_missing",
                credentials={},
                message="缺少 cryptography，无法主动刷新 B站 Cookie",
            )

        effective_timestamp = timestamp_ms
        if effective_timestamp is None:
            check = cls.check_refresh(creds)
            if check.status not in {"ok", "refresh_required"}:
                return AuthRefreshResult(status=check.status, credentials={}, message=check.message)
            effective_timestamp = check.timestamp_ms or int(time.time() * 1000)

        try:
            return cls._refresh(creds, int(effective_timestamp))
        except Exception as e:
            return AuthRefreshResult(status="refresh_failed", credentials={}, message=str(e))

    @classmethod
    def _refresh(cls, credentials: Dict[str, Any], timestamp_ms: int) -> AuthRefreshResult:
        old_token = str(credentials.get("ac_time_value", "")).strip()
        correspond_path = cls._build_correspond_path(timestamp_ms)
        refresh_csrf = cls._fetch_refresh_csrf(correspond_path, credentials)

        form = {
            "csrf": str(credentials["bili_jct"]),
            "refresh_csrf": refresh_csrf,
            "source": "main_web",
            "refresh_token": old_token,
        }
        payload, headers = _post_form(COOKIE_REFRESH_URL, form, credentials)
        if payload.get("code") != 0:
            return AuthRefreshResult(
                status="refresh_failed",
                credentials={},
                message=str(payload.get("message") or f"code={payload.get('code')}"),
            )

        data = payload.get("data") or {}
        new_token = str(data.get("refresh_token") or "").strip()
        if not new_token:
            return AuthRefreshResult(status="refresh_failed", credentials={}, message="刷新响应缺少 refresh_token")

        refreshed = apply_set_cookie(credentials, headers)
        refreshed["ac_time_value"] = new_token
        required = ("SESSDATA", "bili_jct")
        missing = [name for name in required if not refreshed.get(name)]
        if missing:
            return AuthRefreshResult(
                status="refresh_failed",
                credentials={},
                message=f"刷新响应缺少 Cookie: {', '.join(missing)}",
            )

        confirm_form = {"csrf": str(refreshed["bili_jct"]), "refresh_token": old_token}
        confirm_payload, _ = _post_form(COOKIE_CONFIRM_URL, confirm_form, refreshed)
        if confirm_payload.get("code") != 0:
            return AuthRefreshResult(
                status="confirm_failed",
                credentials={},
                message=str(confirm_payload.get("message") or f"code={confirm_payload.get('code')}"),
            )

        refreshed["updated_at"] = int(time.time())
        return AuthRefreshResult(status="refreshed", credentials=refreshed, refreshed=True)

    @staticmethod
    def _build_correspond_path(timestamp_ms: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        public_key = load_pem_public_key(PUBLIC_KEY_PEM)
        encrypted = public_key.encrypt(
            f"refresh_{timestamp_ms}".encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return encrypted.hex()

    @staticmethod
    def _fetch_refresh_csrf(correspond_path: str, credentials: Dict[str, Any]) -> str:
        url = CORRESPOND_URL.format(correspond_path=urllib.parse.quote(correspond_path))
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _user_agent(),
                "Referer": "https://www.bilibili.com/",
                "Cookie": build_cookie_header(credentials),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            html_text = _read_response_text(resp)
        match = re.search(r'<div\s+id=["\']1-name["\']>([^<]+)</div>', html_text, re.IGNORECASE)
        if not match:
            raise ValueError("未能获取 refresh_csrf")
        return html.unescape(match.group(1).strip())


def _read_response_text(resp: Any) -> str:
    data = resp.read()
    encoding = str(resp.headers.get("Content-Encoding") or "").lower()
    try:
        if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
            data = gzip.decompress(data)
        elif encoding == "deflate":
            data = zlib.decompress(data)
    except Exception:
        _logger.debug("响应解压失败，将按原始字节解码", exc_info=True)
    return data.decode("utf-8", errors="ignore")


def _fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _user_agent(),
            "Referer": "https://www.bilibili.com/",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
        text = _read_response_text(resp)
    return json.loads(text)


def _post_form(
    url: str,
    form: Dict[str, str],
    credentials: Dict[str, Any],
) -> tuple[Dict[str, Any], Any]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": _user_agent(),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": build_cookie_header(credentials),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
        payload = json.loads(_read_response_text(resp))
        return payload, resp.headers


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/144.0.0.0 Safari/537.36"
    )
