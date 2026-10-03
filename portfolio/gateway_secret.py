"""
portfolio/gateway_secret.py — Read the rh_gateway LOCAL API secret (not a Robinhood credential).

rh_gateway writes a DPAPI (current-Windows-user) encrypted copy of only its
local API secret to %LOCALAPPDATA%\\rh_gateway_client\\gateway-secret.dpapi.
This module decrypts that one file with the Windows DPAPI via ctypes (no extra
dependency). It never reads the gateway's credential store, which holds the
Robinhood OAuth tokens and the full account number.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Optional

_MAGIC = b"RHGWENC1"
_RECORD = "gateway-secret"
_ENTROPY = b"rh_gateway/credential/v1:" + _RECORD.encode()
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class GatewaySecretUnavailable(RuntimeError):
    pass


def secret_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise GatewaySecretUnavailable("LOCALAPPDATA is not set")
    return Path(base) / "rh_gateway_client" / f"{_RECORD}.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _unprotect(blob: bytes) -> bytes:
    if sys.platform != "win32":
        raise GatewaySecretUnavailable("DPAPI is only available on Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(blob, len(blob))
    ent_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(blob), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    entropy = _Blob(len(_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None,
                                    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise GatewaySecretUnavailable("gateway secret could not be decrypted for this Windows user")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def load_gateway_secret(path: Optional[Path] = None) -> str:
    p = path or secret_path()
    try:
        data = p.read_bytes()
    except OSError:
        raise GatewaySecretUnavailable("gateway secret file not found; start rh_gateway once (python -m "
                                       "rh_gateway serve)") from None
    if not data.startswith(_MAGIC) or len(data) < 12:
        raise GatewaySecretUnavailable("gateway secret file has an unrecognized format")
    hlen = int.from_bytes(data[8:12], "big")
    try:
        header = json.loads(data[12:12 + hlen].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewaySecretUnavailable("gateway secret header unreadable") from None
    if header.get("storage_schema_version") != 1 or header.get("encryption") != "DPAPI_USER" \
            or header.get("record") != _RECORD:
        raise GatewaySecretUnavailable("gateway secret envelope not supported")
    return _unprotect(data[12 + hlen:]).decode("utf-8")
