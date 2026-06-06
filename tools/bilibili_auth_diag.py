# -*- coding: utf-8 -*-
"""Read-only and controlled-refresh diagnostics for Bilibili auth config."""
from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.auth import (  # noqa: E402
    AUTH_CONFIG_FIELD_NAMES,
    BilibiliAuthRefresher,
    AuthRefreshResult,
    normalize_credentials,
    save_auth_config,
)


def _load_credentials(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as file:
        config = tomllib.load(file)
    auth = config.get("auth", {})
    return normalize_credentials(
        {
            name: str(auth.get(name, "") or "").strip()
            for name in AUTH_CONFIG_FIELD_NAMES
        }
    )


def _field_summary(credentials: dict[str, Any]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for name in AUTH_CONFIG_FIELD_NAMES:
        value = str(credentials.get(name, "") or "").strip()
        item: dict[str, Any] = {"present": bool(value), "length": len(value)}
        if value:
            item["suffix"] = value[-4:]
        summary[name] = item
    return summary


def _sessdata_expiry(credentials: dict[str, Any]) -> dict[str, Any] | None:
    sessdata = str(credentials.get("SESSDATA", "") or "").strip()
    if not sessdata:
        return None
    try:
        decoded = urllib.parse.unquote(sessdata)
        parts = decoded.split(",")
        if len(parts) < 2:
            return {"status": "unknown"}
        expiry_ts = int(parts[1])
    except Exception as exc:
        return {"status": "parse_failed", "message": str(exc)}

    now = int(time.time())
    return {
        "status": "expired" if expiry_ts < now else "future",
        "timestamp": expiry_ts,
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry_ts)),
        "seconds_from_now": expiry_ts - now,
    }


def _refresh_result_payload(result: AuthRefreshResult, wrote_config: bool) -> dict[str, Any]:
    return {
        "status": result.status,
        "ok": result.ok,
        "refreshed": result.refreshed,
        "message": result.message,
        "wrote_config": wrote_config,
        "updated_fields": _field_summary(result.credentials) if result.credentials else {},
    }


def _check_payload(config_path: Path, credentials: dict[str, Any]) -> dict[str, Any]:
    result = BilibiliAuthRefresher.check_refresh(credentials)
    return {
        "mode": "check",
        "config_path": str(config_path),
        "cryptography_available": BilibiliAuthRefresher.cryptography_available(),
        "fields": _field_summary(credentials),
        "sessdata_expiry": _sessdata_expiry(credentials),
        "status": result.status,
        "refresh_required": result.refresh_required,
        "message": result.message,
        "timestamp_ms_present": bool(result.timestamp_ms),
        "wrote_config": False,
    }


def _force_refresh_payload(
    config_path: Path,
    credentials: dict[str, Any],
    write_config: bool,
    confirmed: bool,
) -> dict[str, Any]:
    if not write_config or not confirmed:
        return {
            "mode": "force_refresh",
            "config_path": str(config_path),
            "status": "confirmation_required",
            "ok": False,
            "message": (
                "Force refresh requires both --write-config and "
                "--i-understand-this-refreshes-cookie."
            ),
            "wrote_config": False,
        }

    result = BilibiliAuthRefresher.force_refresh(credentials)
    wrote_config = False
    if result.ok and result.refreshed and result.credentials:
        save_auth_config(str(config_path), result.credentials)
        wrote_config = True

    payload = {
        "mode": "force_refresh",
        "config_path": str(config_path),
        "cryptography_available": BilibiliAuthRefresher.cryptography_available(),
        "input_fields": _field_summary(credentials),
        "input_sessdata_expiry": _sessdata_expiry(credentials),
    }
    payload.update(_refresh_result_payload(result, wrote_config))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose or controlled-refresh Bilibili auth config.")
    parser.add_argument(
        "--config",
        default=str(PLUGIN_ROOT / "config.toml"),
        help="Path to plugin config.toml.",
    )
    parser.add_argument("--check", action="store_true", help="Run read-only /cookie/info check.")
    parser.add_argument("--force-refresh", action="store_true", help="Run a real refresh flow.")
    parser.add_argument("--write-config", action="store_true", help="Write refreshed credentials to config.toml.")
    parser.add_argument(
        "--i-understand-this-refreshes-cookie",
        action="store_true",
        help="Required confirmation for --force-refresh.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    credentials = _load_credentials(config_path)

    if args.force_refresh:
        payload = _force_refresh_payload(
            config_path,
            credentials,
            write_config=bool(args.write_config),
            confirmed=bool(args.i_understand_this_refreshes_cookie),
        )
    else:
        payload = _check_payload(config_path, credentials)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("status") in {"confirmation_required", "invalid", "dependency_missing", "missing_material"}:
        return 1
    if payload.get("mode") == "force_refresh" and not payload.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
