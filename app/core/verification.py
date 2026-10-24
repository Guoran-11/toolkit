"""邮箱 / 短信验证码的签发与校验。

安全设计要点：
    1. **只存哈希**。数据库里存的是 ``HMAC-SHA256(应用密钥, 目标|用途|验证码)``，
       因此即使 ``toolkit.db`` 泄露，攻击者也无法直接读出验证码；密钥不在库里，
       离线爆破 6 位数字需要先拿到 ``storage/.secret``。
    2. **常数时间比较**，避免通过响应耗时逐位试探。
    3. **三重限流**：同目标重发间隔、同目标每小时上限、同 IP 每小时上限。
    4. **单次消费**：校验通过立即置 ``consumed``，杜绝重放。
    5. **尝试次数上限**：超过后该验证码作废，需重新获取。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Literal

from app.core.config import get_settings
from app.core.db import get_connection, transaction
from app.core.errors import RateLimitedError, ValidationError
from app.core.mailer import send_verification_code as send_mail_code
from app.core.security import secret_store
from app.core.sms import send_verification_code as send_sms_code

logger = logging.getLogger(__name__)

Channel = Literal["email", "sms"]
Purpose = Literal["register", "login", "reset"]

CHANNELS: frozenset[str] = frozenset({"email", "sms"})
PURPOSES: frozenset[str] = frozenset({"register", "login", "reset"})

# 验证码位数（6 位是可用性与安全性的通行折中：10^6 空间 + 5 次尝试上限）
_CODE_DIGITS = 6

# 同一目标每小时的发码上限（独立于 IP 限制）
_TARGET_HOURLY_LIMIT = 10


def _generate_code() -> str:
    """生成密码学安全的数字验证码。

    Returns:
        形如 ``"042317"`` 的定长数字串。
    """
    upper = 10**_CODE_DIGITS
    return str(secrets.randbelow(upper)).zfill(_CODE_DIGITS)


def _hash_code(target: str, purpose: str, code: str) -> str:
    """对验证码做带密钥的哈希。

    Args:
        target: 目标（邮箱或手机号）。
        purpose: 用途。
        code: 明文验证码。

    Returns:
        十六进制摘要。
    """
    message = f"{target.strip().lower()}|{purpose}|{code}".encode()
    return hmac.new(secret_store.secret, message, hashlib.sha256).hexdigest()


def _count_recent(target: str, *, window_sec: int, ip: str | None = None) -> int:
    """统计窗口内的发码次数。

    Args:
        target: 目标。
        window_sec: 时间窗口（秒）。
        ip: 若提供则按 IP 统计，否则按目标统计。

    Returns:
        次数。
    """
    since = time.time() - window_sec
    conn = get_connection()
    if ip is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM verify_codes WHERE ip = ? AND created_at >= ?",
            (ip, since),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM verify_codes WHERE target = ? COLLATE NOCASE AND created_at >= ?",
            (target.strip().lower(), since),
        ).fetchone()
    return int(row["n"])


def _seconds_since_last(target: str, purpose: str) -> float | None:
    """取该目标+用途最近一次发码距今的秒数。

    Args:
        target: 目标。
        purpose: 用途。

    Returns:
        秒数；从未发过返回 None。
    """
    row = get_connection().execute(
        """
        SELECT created_at FROM verify_codes
        WHERE target = ? COLLATE NOCASE AND purpose = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (target.strip().lower(), purpose),
    ).fetchone()
    return (time.time() - float(row["created_at"])) if row else None


def issue(*, target: str, channel: Channel, purpose: Purpose, ip: str | None = None) -> str:
    """签发并发送验证码。

    Args:
        target: 邮箱或手机号。
        channel: ``email`` 或 ``sms``。
        purpose: ``register`` / ``login`` / ``reset``。
        ip: 请求来源 IP（用于限流）。

    Returns:
        明文验证码（仅用于日志发送器与测试断言，不返回给客户端）。

    Raises:
        ValidationError: 通道或用途非法、目标格式不合法。
        RateLimitedError: 触发重发间隔或配额限制。
        MailDeliveryError: 邮件投递失败。
        SmsDeliveryError: 短信下发失败。
    """
    settings = get_settings()
    auth_cfg = settings.auth

    if channel not in CHANNELS:
        raise ValidationError(f"不支持的验证码通道：{channel}")
    if purpose not in PURPOSES:
        raise ValidationError(f"不支持的验证码用途：{purpose}")

    target = (target or "").strip().lower()
    if not target:
        raise ValidationError("接收验证码的地址不能为空")

    # ---- 限流：重发间隔 ----
    elapsed = _seconds_since_last(target, purpose)
    if elapsed is not None and elapsed < auth_cfg.code_resend_sec:
        wait = int(auth_cfg.code_resend_sec - elapsed) + 1
        raise RateLimitedError(
            f"验证码发送过于频繁，请在 {wait} 秒后重试。",
            extra={"retry_after": wait},
        )

    # ---- 限流：目标小时配额 ----
    if _count_recent(target, window_sec=3600) >= _TARGET_HOURLY_LIMIT:
        raise RateLimitedError(
            "该地址一小时内获取验证码次数过多，请稍后再试。",
            extra={"retry_after": 3600},
        )

    # ---- 限流：IP 小时配额 ----
    if ip and _count_recent(target, window_sec=3600, ip=ip) >= auth_cfg.code_ip_hourly_limit:
        raise RateLimitedError(
            "当前网络获取验证码次数过多，请稍后再试。",
            extra={"retry_after": 3600},
        )

    code = _generate_code()
    now = time.time()
    expires_at = now + auth_cfg.code_ttl_sec

    with transaction() as conn:
        # 作废该目标+用途下所有未消费的旧验证码：新码一旦发出，旧码立即失效，
        # 避免"连点两次拿到两个都能用"的混乱
        conn.execute(
            """
            UPDATE verify_codes SET consumed = 1
            WHERE target = ? COLLATE NOCASE AND purpose = ? AND consumed = 0
            """,
            (target, purpose),
        )
        conn.execute(
            """
            INSERT INTO verify_codes (target, channel, purpose, code_hash, expires_at, ip, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (target, channel, purpose, _hash_code(target, purpose, code), expires_at, ip, now),
        )

    # 发送放在事务之外：SMTP/HTTP 可能耗时数秒，不该占着写锁
    if channel == "email":
        send_mail_code(to=target, code=code, purpose=purpose, ttl_sec=auth_cfg.code_ttl_sec)
    else:
        send_sms_code(to=target, code=code, purpose=purpose, ttl_sec=auth_cfg.code_ttl_sec)

    return code


def consume(*, target: str, purpose: Purpose, code: str) -> None:
    """校验并消费验证码。

    Args:
        target: 目标。
        purpose: 用途。
        code: 用户提交的验证码。

    Raises:
        ValidationError: 无有效验证码、已过期、尝试过多或验证码不正确。
    """
    settings = get_settings()
    max_attempts = settings.auth.code_max_attempts

    normalized = (target or "").strip().lower()
    submitted = (code or "").strip()
    if not normalized or not submitted:
        raise ValidationError("验证码不能为空")

    now = time.time()
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT id, code_hash, expires_at, attempts FROM verify_codes
            WHERE target = ? COLLATE NOCASE AND purpose = ? AND consumed = 0
            ORDER BY created_at DESC LIMIT 1
            """,
            (normalized, purpose),
        ).fetchone()

        if row is None:
            raise ValidationError("验证码无效或已使用，请重新获取。")

        if float(row["expires_at"]) < now:
            conn.execute("UPDATE verify_codes SET consumed = 1 WHERE id = ?", (row["id"],))
            raise ValidationError("验证码已过期，请重新获取。")

        if int(row["attempts"]) >= max_attempts:
            conn.execute("UPDATE verify_codes SET consumed = 1 WHERE id = ?", (row["id"],))
            raise ValidationError("验证码尝试次数过多，请重新获取。")

        expected = str(row["code_hash"])
        if not hmac.compare_digest(expected, _hash_code(normalized, purpose, submitted)):
            conn.execute("UPDATE verify_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            remaining = max_attempts - int(row["attempts"]) - 1
            raise ValidationError(
                f"验证码不正确，还可尝试 {remaining} 次。" if remaining > 0 else "验证码不正确，请重新获取。"
            )

        conn.execute("UPDATE verify_codes SET consumed = 1 WHERE id = ?", (row["id"],))


def purge_expired(*, older_than_sec: int = 86400) -> int:
    """清理过期验证码记录（由后台维护循环调用）。

    Args:
        older_than_sec: 保留时长（秒），默认 1 天。

    Returns:
        清理条数。
    """
    cutoff = time.time() - older_than_sec
    with transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM verify_codes WHERE expires_at < ? AND created_at < ?",
            (time.time(), cutoff),
        )
    return int(cursor.rowcount or 0)


__all__ = ["CHANNELS", "PURPOSES", "Channel", "Purpose", "consume", "issue", "purge_expired"]
