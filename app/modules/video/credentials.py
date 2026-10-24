"""平台 Cookie 的加密落盘与脱敏。

契约 §5.4：
    · 文件 ``storage/credentials.json``，结构
      ``{"version":1,"items":{"bilibili":{"nonce":...,"ciphertext":...,"tag":...,"updated_at":...}}}``，
      权限 0600；
    · 密钥由 ``app.core.security.secret_store.secret`` 经 HMAC 派生；
    · 优先使用 ``cryptography`` 的 AES-GCM；未安装时回退到本模块的 stdlib 实现；
    · 接口响应与日志**永不**出现明文 Cookie。

关于回退实现（务必知悉其安全边界）：
    这是「无第三方依赖的降级实现」——HMAC-SHA256 派生密钥 + 计数器模式异或 +
    HMAC 校验。它能防止凭据以明文躺在磁盘上（例如被误打包、被日志采集工具抓走），
    但**不是**经过审计的成熟 AEAD 实现，不提供 AES-GCM 那样的标准化保证。
    因此它仅用于本机离线存储，且文件权限收紧到 0600；生产环境建议安装
    ``cryptography`` 走 AES-GCM 分支（本仓库的 ``[project.optional-dependencies].full``
    已包含它）。两种实现通过 ``_seal``/``_unseal`` 自动选择，数据格式保持一致。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Final

from app.core.config import STORAGE_DIR
from app.core.security import secret_store
from app.core.tasks import utc_now_iso

logger = logging.getLogger(__name__)

_CREDENTIALS_FILE: Final[Path] = STORAGE_DIR / "credentials.json"
_FORMAT_VERSION: Final[int] = 1
_KEY_CONTEXT: Final[bytes] = b"toolkit-video-credentials-v1"
_KNOWN_PLATFORMS: Final[tuple[str, ...]] = ("douyin", "bilibili", "kuaishou")

_lock = threading.RLock()
_items: dict[str, dict[str, Any]] | None = None


# ---------------------------------------------------------------- 加解密


def _derive_key() -> bytes:
    """从进程密钥派生 32 字节加密密钥。

    Returns:
        32 字节密钥。
    """
    return hmac.new(secret_store.secret, _KEY_CONTEXT, hashlib.sha256).digest()


def _xor_keystream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """用 HMAC-SHA256 计数器模式生成密钥流并与数据异或。

    Args:
        key: 32 字节密钥。
        nonce: 16 字节随机数。
        data: 待处理数据。

    Returns:
        与输入等长的变换结果。
    """
    output = bytearray()
    for counter, offset in enumerate(range(0, len(data), 32)):
        block = hmac.new(key, b"stream" + nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[offset : offset + 32]
        output.extend(a ^ b for a, b in zip(chunk, block, strict=False))
    return bytes(output)


def _seal(platform: str, cookie: str) -> dict[str, Any]:
    """加密 Cookie。

    Args:
        platform: 平台标识。
        cookie: 明文 Cookie（仅在本函数内存中短暂存在）。

    Returns:
        含 ``nonce`` / ``ciphertext`` / ``tag`` / ``updated_at`` 的记录。
    """
    plaintext = cookie.encode("utf-8")
    nonce = secrets.token_bytes(16)

    try:
        # 优先 AES-GCM（cryptography 为可选依赖，未安装则走回退实现）
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _derive_key()
        sealed = AESGCM(key).encrypt(nonce, plaintext, platform.encode("utf-8"))
        ciphertext, tag = sealed[:-16], sealed[-16:]
    except ImportError:
        key = _derive_key()
        body = _xor_keystream(key, nonce, plaintext)
        tag = hmac.new(key, b"tag" + nonce + body, hashlib.sha256).digest()[:16]
        ciphertext = body

    return {
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "tag": tag.hex(),
        "updated_at": utc_now_iso(),
    }


def _unseal(platform: str, entry: dict[str, Any]) -> str:
    """解密 Cookie。

    Args:
        platform: 平台标识。
        entry: 加密记录。

    Returns:
        明文 Cookie。

    Raises:
        ValueError: 记录结构非法或校验失败（错误信息**不含**任何密文/明文）。
    """
    try:
        nonce = bytes.fromhex(str(entry["nonce"]))
        ciphertext = bytes.fromhex(str(entry["ciphertext"]))
        tag = bytes.fromhex(str(entry["tag"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("凭据记录结构非法") from exc

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _derive_key()
        return AESGCM(key).decrypt(nonce, ciphertext + tag, platform.encode("utf-8")).decode("utf-8")
    except ImportError:
        key = _derive_key()
        expected = hmac.new(key, b"tag" + nonce + ciphertext, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, tag):
            raise ValueError("凭据校验失败（密钥已变更或文件被篡改）") from None
        return _xor_keystream(key, nonce, ciphertext).decode("utf-8")
    except Exception as exc:
        raise ValueError("凭据解密失败") from exc


# ---------------------------------------------------------------- 文件读写


def _load() -> dict[str, dict[str, Any]]:
    """载入凭据文件（惰性、单例）。

    Returns:
        ``platform -> 加密记录`` 的字典；文件缺失或损坏时返回空字典。
    """
    global _items
    with _lock:
        if _items is not None:
            return _items
        _items = {}
        if not _CREDENTIALS_FILE.exists():
            return _items
        try:
            raw = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("凭据文件损坏，已忽略（不会泄露内容）：%s", exc)
            return _items
        items = raw.get("items") if isinstance(raw, dict) else None
        if isinstance(items, dict):
            for platform, entry in items.items():
                if isinstance(entry, dict) and "ciphertext" in entry:
                    _items[str(platform)] = dict(entry)
        return _items


def _flush(platform: str, entry: dict[str, Any] | None) -> None:
    """写回凭据文件（原子替换 + 0600 权限）。

    Args:
        platform: 平台标识。
        entry: 加密记录；None 表示删除该平台。
    """
    data = _load()
    if entry is None:
        data.pop(platform, None)
    else:
        data[platform] = entry

    try:
        _CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CREDENTIALS_FILE.with_suffix(".tmp")
        payload = {"version": _FORMAT_VERSION, "items": data}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _CREDENTIALS_FILE)
        # Windows 上 chmod 语义有限，忽略即可（文档已说明）
        with contextlib.suppress(OSError):
            os.chmod(_CREDENTIALS_FILE, 0o600)
    except OSError as exc:
        logger.warning("凭据文件写入失败：%s", exc)


# ---------------------------------------------------------------- 对外接口


def save_cookie(platform: str, cookie: str) -> dict[str, Any]:
    """保存（或清除）平台 Cookie。

    Args:
        platform: 平台标识。
        cookie: 明文 Cookie；空字符串表示清除。

    Returns:
        该平台的脱敏状态 ``{"configured", "masked", "updated_at"}``。
    """
    name = (platform or "").strip().lower()
    text = (cookie or "").strip()
    with _lock:
        if not text:
            _flush(name, None)
            return {"configured": False, "masked": None, "updated_at": None}
        entry = _seal(name, text)
        _flush(name, entry)
        return {
            "configured": True,
            "masked": mask_cookie(name, text),
            "updated_at": entry["updated_at"],
        }


def get_cookie(platform: str) -> str | None:
    """读取明文 Cookie（仅供请求构造使用，调用方不得记录日志）。

    Args:
        platform: 平台标识。

    Returns:
        明文 Cookie；未配置或解密失败返回 None。
    """
    name = (platform or "").strip().lower()
    entry = _load().get(name)
    if not entry:
        return None
    try:
        return _unseal(name, entry)
    except ValueError as exc:
        # 这里刻意只记录平台与原因，绝不记录密文或明文
        logger.warning("平台 %s 的 Cookie 无法解密：%s", name, exc)
        return None


def clear_cookie(platform: str) -> bool:
    """删除平台 Cookie。

    Args:
        platform: 平台标识。

    Returns:
        是否存在并删除了记录。
    """
    name = (platform or "").strip().lower()
    with _lock:
        existed = name in _load()
        if existed:
            _flush(name, None)
        return existed


def cookie_status() -> dict[str, dict[str, Any]]:
    """导出全部平台的脱敏 Cookie 状态。

    Returns:
        ``platform -> {"configured", "masked", "updated_at"}``，含三个已知平台占位。
    """
    data = _load()
    result: dict[str, dict[str, Any]] = {
        name: {"configured": False, "masked": None, "updated_at": None} for name in _KNOWN_PLATFORMS
    }
    for name, entry in data.items():
        cookie = get_cookie(name)
        status = result.get(name, {"configured": False, "masked": None, "updated_at": None})
        if cookie:
            status = {
                "configured": True,
                "masked": mask_cookie(name, cookie),
                "updated_at": entry.get("updated_at"),
            }
        result[name] = status
    return result


def mask_cookie(platform: str, cookie: str) -> str:
    """返回 Cookie 的脱敏展示串。

    规则（契约 §5.4）：
        · B站：定位 ``SESSDATA=...`` 输出 ``SESSDATA=****末4位``；
        · 其它平台：取前 4 个键名 + ``=****末4位``；无 ``=`` 时 ``****末4位``。

    Args:
        platform: 平台标识。
        cookie: 明文 Cookie。

    Returns:
        脱敏串；空 Cookie 返回空字符串。
    """
    text = (cookie or "").strip()
    if not text:
        return ""
    tail = text[-4:]

    if (platform or "").lower() == "bilibili" or "SESSDATA=" in text.upper():
        for part in text.split(";"):
            stripped = part.strip()
            if stripped.upper().startswith("SESSDATA="):
                value = stripped.split("=", 1)[1].strip()
                return f"SESSDATA=****{value[-4:]}" if value else "SESSDATA=****"
        return f"****{tail}"

    if "=" in text:
        keys: list[str] = []
        for part in text.split(";"):
            key = part.split("=", 1)[0].strip()
            if key:
                keys.append(key)
            if len(keys) >= 4:
                break
        if keys:
            return f"{','.join(keys)}=****{tail}"
    return f"****{tail}"


def reset_for_tests() -> None:
    """清空内存缓存（测试替换 storage 目录后调用）。"""
    global _items
    with _lock:
        _items = None


__all__ = [
    "clear_cookie",
    "cookie_status",
    "get_cookie",
    "mask_cookie",
    "reset_for_tests",
    "save_cookie",
]
