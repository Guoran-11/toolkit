"""邮件通道最终验证：连接 → 认证 → 真实发信（一条命令跑完）。

用法::

    python scripts/check_mail.py                    # 发给自己（noreply@example.com）
    python scripts/check_mail.py you@example.com    # 发到指定收件人

成功时会真正投递一封验证码邮件，可用于确认整条链路（含垃圾箱判定）。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.mailer import MailDeliveryError, get_mailer, is_configured, send_verification_code  # noqa: E402


def _ssl_context(*, verify: bool) -> ssl.SSLContext:
    """构造 TLS 上下文。"""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def main(argv: list[str]) -> int:
    """执行验证。

    Args:
        argv: 命令行参数（可选收件人）。

    Returns:
        进程退出码。
    """
    cfg = get_settings().email
    recipient = argv[1] if len(argv) > 1 else (cfg.from_address or cfg.username)

    print("=== 配置 ===")
    print(f"  enabled      : {cfg.enabled}")
    print(f"  host:port    : {cfg.host}:{cfg.port} ({cfg.security})")
    print(f"  username     : {cfg.username}")
    print(f"  密码已设置    : {bool(cfg.password)}（长度 {len(cfg.password)}）")
    print(f"  校验证书      : {cfg.tls_verify}")
    print(f"  收件人        : {recipient}")
    print()

    print("=== 1. 连接与认证 ===")
    try:
        ctx = _ssl_context(verify=cfg.tls_verify)
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout_sec, context=ctx)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_sec)
        with server:
            server.ehlo()
            if cfg.security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            server.login(cfg.username, cfg.password)
        print("  [OK]   认证成功")
    except smtplib.SMTPAuthenticationError as exc:
        raw = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        print(f"  [FAIL] 认证被拒：{exc.smtp_code} {raw}")
        print("         → 账号或密码不正确，或该邮箱尚未在邮件服务器上真正创建。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        return 1

    print()
    print("=== 2. 真实发信 ===")
    if not is_configured(cfg):
        print("  [FAIL] 通道未配置（email.enabled=false 或 host 为空）")
        return 1
    print(f"  发送器: {get_mailer(cfg).name}")
    try:
        send_verification_code(to=recipient, code="246813", purpose="register", ttl_sec=300)
    except MailDeliveryError as exc:
        print(f"  [FAIL] {exc.detail}")
        return 1
    print(f"  [OK]   已投递到 {recipient}")
    print()
    print("请到收件箱确认（同时检查垃圾邮件目录）。若没收到，")
    print("多半是发件域名的 SPF/DKIM 记录未配置，导致被对方拒收或判为垃圾邮件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
