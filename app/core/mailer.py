"""发信通道：标准 SMTP。

为什么用标准库 ``smtplib`` 而不是第三方邮件 SDK：
    任何邮件服务（自建 BillionMail / 企业邮箱 / 阿里云邮件推送 / SendGrid）
    都提供 SMTP 提交端口，``smtplib`` 一把梭即可，且零新增依赖。
    这与项目「核心依赖尽量少」的取向一致。

两种发送器：
    · :class:`SmtpMailSender` —— 真实发信（``email.enabled = true``）
    · :class:`LogMailSender`  —— 只写日志（默认）。用于本地联调与「尚未配置
      SMTP」的部署：验证码会打到日志里，管理员可以手工转达；同时会在
      ``/api/auth/config`` 里标记 ``email_configured = false``，
      前端据此提示「邮件服务未配置」。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Protocol

from app.core.config import EmailConfig, get_settings
from app.core.errors import ToolkitError

logger = logging.getLogger(__name__)


class MailDeliveryError(ToolkitError):
    """邮件投递失败（SMTP 不可达、认证失败、被拒收）。"""

    status_code = 502
    code = "MAIL_DELIVERY_FAILED"


class MailSender(Protocol):
    """发送器协议。"""

    name: str

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        """发送一封邮件。

        Args:
            to: 收件人地址。
            subject: 主题。
            text: 纯文本正文。
            html: HTML 正文（可选）。

        Raises:
            MailDeliveryError: 投递失败。
        """
        ...


def _build_message(cfg: EmailConfig, *, to: str, subject: str, text: str, html: str | None) -> EmailMessage:
    """组装 MIME 邮件（纯文本 + 可选 HTML 的 multipart/alternative）。

    Args:
        cfg: 邮件配置。
        to: 收件人。
        subject: 主题。
        text: 纯文本正文。
        html: HTML 正文。

    Returns:
        组装好的 EmailMessage。
    """
    sender = cfg.from_address or cfg.username
    msg = EmailMessage()
    # 用 Address 而不是手工拼 "名字 <地址>"：中文发件人名需要 RFC 2047 编码，
    # 手工拼接会产出非法头，部分邮件服务会直接拒收。
    msg["From"] = Address(display_name=cfg.from_name or "ToolKit", addr_spec=sender)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _ssl_context(cfg: EmailConfig) -> ssl.SSLContext:
    """构造 TLS 上下文。

    自建邮件服务器常用自签名证书，``tls_verify=false`` 时跳过证书校验；
    否则用系统默认的受信 CA 集合严格校验。

    Args:
        cfg: 邮件配置。

    Returns:
        配置好的 SSLContext。
    """
    if cfg.tls_verify:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class SmtpMailSender:
    """基于标准库 smtplib 的发送器。"""

    name = "smtp"

    def __init__(self, cfg: EmailConfig) -> None:
        """初始化。

        Args:
            cfg: 邮件配置。
        """
        self._cfg = cfg

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        """发送邮件。

        Args:
            to: 收件人。
            subject: 主题。
            text: 纯文本正文。
            html: HTML 正文。

        Raises:
            MailDeliveryError: 投递失败（错误信息已脱敏，不含密码）。
        """
        cfg = self._cfg
        msg = _build_message(cfg, to=to, subject=subject, text=text, html=html)
        try:
            if cfg.security == "ssl":
                with smtplib.SMTP_SSL(
                    cfg.host, cfg.port, timeout=cfg.timeout_sec, context=_ssl_context(cfg)
                ) as server:
                    self._login_and_send(server, msg)
            else:
                with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_sec) as server:
                    server.ehlo()
                    if cfg.security == "starttls":
                        server.starttls(context=_ssl_context(cfg))
                        server.ehlo()
                    self._login_and_send(server, msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailDeliveryError(
                "SMTP 认证失败，请检查邮箱账号与授权码/密码是否正确。"
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            # 自签名证书是最常见的自建邮件服务器坑，给出可执行的指引
            raise MailDeliveryError(
                "SMTP 服务器证书校验失败（多为自签名证书）。"
                "若这是你自己的邮件服务器，请在 config.toml 的 [email] 段设置 tls_verify = false；"
                "否则请为邮件服务器申请受信任的证书。"
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            # 只回显异常类型与简短原因，避免把凭据带进响应或日志
            raise MailDeliveryError(f"邮件发送失败：{type(exc).__name__}: {exc}") from exc
        logger.info("验证邮件已投递：%s", to)

    def _login_and_send(self, server: smtplib.SMTP, msg: EmailMessage) -> None:
        """按需登录并发送。

        Args:
            server: 已连接的 SMTP 会话。
            msg: 待发送邮件。
        """
        cfg = self._cfg
        if cfg.username:
            server.login(cfg.username, cfg.password)
        server.send_message(msg)


class LogMailSender:
    """只写日志的发送器（未配置 SMTP 时的降级实现）。"""

    name = "log"

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        """把邮件内容写入日志。

        Args:
            to: 收件人。
            subject: 主题。
            text: 纯文本正文。
            html: HTML 正文（忽略）。
        """
        del html
        logger.warning(
            "【邮件未配置 SMTP，仅记录不外发】收件人=%s 主题=%s\n%s",
            to,
            subject,
            text,
        )


def get_mailer(cfg: EmailConfig | None = None) -> MailSender:
    """按配置返回发送器。

    Args:
        cfg: 邮件配置；默认取全局配置。

    Returns:
        MailSender 实例。
    """
    email_cfg = cfg or get_settings().email
    if email_cfg.enabled and email_cfg.host:
        return SmtpMailSender(email_cfg)
    return LogMailSender()


def is_configured(cfg: EmailConfig | None = None) -> bool:
    """邮件通道是否已真正配置。

    Args:
        cfg: 邮件配置；默认取全局配置。

    Returns:
        已启用且有 SMTP 主机时返回 True。
    """
    email_cfg = cfg or get_settings().email
    return bool(email_cfg.enabled and email_cfg.host)


# ---------------------------------------------------------------- 模板

_PURPOSE_LABEL = {
    "register": "注册账号",
    "login": "登录验证",
    "reset": "重置密码",
}


def render_verification_email(*, code: str, purpose: str, ttl_sec: int, site_url: str = "") -> tuple[str, str, str]:
    """渲染验证码邮件。

    Args:
        code: 验证码。
        purpose: 用途（register/login/reset）。
        ttl_sec: 有效期（秒）。
        site_url: 站点地址（用于署名）。

    Returns:
        ``(主题, 纯文本正文, HTML 正文)``。
    """
    label = _PURPOSE_LABEL.get(purpose, "身份验证")
    minutes = max(1, ttl_sec // 60)
    subject = f"【全能工具站】{label}验证码：{code}"

    text = (
        f"你正在进行「{label}」。\n\n"
        f"验证码：{code}\n"
        f"有效期：{minutes} 分钟\n\n"
        "如果不是你本人操作，请忽略本邮件，无需任何处理。\n"
        "请勿将验证码转发给他人。\n"
    )
    if site_url:
        text += f"\n站点：{site_url}\n"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f4f6fa;font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1f2733;">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;padding:28px 32px;">
    <h2 style="margin:0 0 6px;font-size:18px;">{label}</h2>
    <p style="margin:0 0 20px;color:#6b7280;font-size:14px;">请在页面中输入下面的验证码完成验证。</p>
    <div style="font-size:32px;font-weight:700;letter-spacing:8px;padding:16px 0;text-align:center;
                background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;">{code}</div>
    <p style="margin:18px 0 0;color:#6b7280;font-size:13px;">有效期 {minutes} 分钟。请勿转发给他人。</p>
    <hr style="border:none;border-top:1px solid #eef1f5;margin:22px 0;">
    <p style="margin:0;color:#9ca3af;font-size:12px;">
      如果这不是你本人的操作，忽略本邮件即可，你的账号不会有任何变化。
    </p>
  </div>
</body></html>"""
    return subject, text, html


def send_verification_code(*, to: str, code: str, purpose: str, ttl_sec: int) -> None:
    """发送验证码邮件。

    Args:
        to: 收件人邮箱。
        code: 验证码。
        purpose: 用途。
        ttl_sec: 有效期（秒）。

    Raises:
        MailDeliveryError: 投递失败。
    """
    settings = get_settings()
    subject, text, html = render_verification_email(
        code=code,
        purpose=purpose,
        ttl_sec=ttl_sec,
        site_url=settings.email.site_url,
    )
    get_mailer().send(to=to, subject=subject, text=text, html=html)


__all__ = [
    "LogMailSender",
    "MailDeliveryError",
    "MailSender",
    "SmtpMailSender",
    "get_mailer",
    "is_configured",
    "render_verification_email",
    "send_verification_code",
]
