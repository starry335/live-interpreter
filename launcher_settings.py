"""Local preferences with user-scoped Windows DPAPI protection for the API key."""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import tempfile


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("当前平台尚未提供 API Key 加密存储。")

    class Blob(C.Structure):
        _fields_ = [("size", W.DWORD), ("data", C.c_void_p)]

    crypt = C.WinDLL("crypt32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    blob_pointer = C.POINTER(Blob)
    crypt.CryptProtectData.argtypes = [blob_pointer, W.LPCWSTR, blob_pointer, C.c_void_p, C.c_void_p, W.DWORD, blob_pointer]
    crypt.CryptUnprotectData.argtypes = [blob_pointer, C.c_void_p, blob_pointer, C.c_void_p, C.c_void_p, W.DWORD, blob_pointer]
    crypt.CryptProtectData.restype = crypt.CryptUnprotectData.restype = W.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    buffer = C.create_string_buffer(data)
    source = Blob(len(data), C.cast(buffer, C.c_void_p))
    result = Blob()
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    description = "Live Interpreter API Key" if protect else None
    # CRYPTPROTECT_UI_FORBIDDEN; omit LOCAL_MACHINE to bind protection to this user.
    if not function(C.byref(source), description, None, None, None, 1, C.byref(result)):
        raise C.WinError(C.get_last_error())
    try:
        return C.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def protect_api_key(value: str) -> str:
    return "dpapi:" + base64.b64encode(_crypt(value.encode("utf-8"), True)).decode("ascii") if value else ""


def unprotect_api_key(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("dpapi:"):
        raise ValueError("无法识别 API Key 加密格式。")
    return _crypt(base64.b64decode(value[6:], validate=True), False).decode("utf-8")


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.stat().st_size > 1_000_000:
        raise ValueError("配置文件过大。")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("配置文件格式错误。")
    return value


def save(path: Path, value: dict) -> None:
    if "aliyun_api_key" in value:
        raise ValueError("API Key 必须加密后保存。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
