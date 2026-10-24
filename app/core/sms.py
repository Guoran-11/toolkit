"""短信通道（可插拔）。

默认 ``provider = "log"``：只写日志，不依赖任何凭据。
需要真实下发时，把 ``provider`` 换成 ``webhook``，由自建网关对接具体厂商
（阿里云 / 腾讯云 / 华为云），应用侧只发一个 JSON。

为什么不在应用内直接实现厂商签名：
    国内短信厂商的签名算法（阿里云 HMAC-SHA1、腾讯云 TC3-HMAC-SHA256）
    差异大且会演进，还要处理签名报备、模板审核、失败重试与并发限流。
    把这段放在一个独立网关里，应用只依赖「HTTP + JSON」这一稳定契约，
    既避免在应用内引入厂商 SDK，也让换厂商变成改网关而不是改应用。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

from app.core.config import SmsConfig, get_settings
from app.core.errors import ToolkitError

logger = logging.getLogger(__name__)

_PURPOSE_LABEL = {
    "register": "注册账号",
    "login": "登录验证",
    "reset": "重置密码",
}


class SmsDeliveryError(ToolkitError):
    """短信下发失败。"""

    status_code = 502
    code = "SMS_DELIVERY_FAILED"


class SmsSender(Protocol):
    """短信发送器协议。"""

    name: str

    def send(self, *, to: str, code: str, purpose: str, ttl_sec: int) -> None:
        """下发验证码短信。

        Args:
            to: 手机号。
            code: 验证码。
            purpose: 用途。
            ttl_sec: 有效期（秒）。

        Raises:
            SmsDeliveryError: 下发失败。
        """
        ...


class LogSmsSender:
    """只写日志的发送器（默认实现）。"""

    name = "log"

    def send(self, *, to: str, code: str, purpose: str, ttl_sec: int) -> None:
        """把短信内容写入日志。

        Args:
            to: 手机号。
            code: 验证码。
            purpose: 用途。
            ttl_sec: 有效期（秒）。
        """
        logger.warning(
            "【短信未配置，仅记录不外发】手机号=%s 用途=%s 验证码=%s（%d 秒内有效）",
            to,
            _PURPOSE_LABEL.get(purpose, purpose),
            code,
            ttl_sec,
        )


class WebhookSmsSender:
    """把验证码 POST 给自建网关。"""

    name = "webhook"

    def __init__(self, cfg: SmsConfig) -> None:
        """初始化。

        Args:
            cfg: 短信配置。
        """
        self._cfg = cfg

    def send(self, *, to: str, code: str, purpose: str, ttl_sec: int) -> None:
        """POST 到 webhook。

        Args:
            to: 手机号。
            code: 验证码。
            purpose: 用途。
            ttl_sec: 有效期（秒）。

        Raises:
            SmsDeliveryError: 网关不可达或返回非 2xx。
        """
        cfg = self._cfg
        if not cfg.webhook_url:
            raise SmsDeliveryError("短信网关地址未配置（sms.webhook_url）")

        payload: dict[str, Any] = {
            "to": to,
            "code": code,
            "purpose": purpose,
            "purpose_label": _PURPOSE_LABEL.get(purpose, purpose),
            "ttl_sec": ttl_sec,
            "sign_name": cfg.sign_name,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if cfg.webhook_token:
            headers["Authorization"] = f"Bearer {cfg.webhook_token}"

        request = urllib.request.Request(cfg.webhook_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout_sec) as response:
                if not (200 <= response.status < 300):
                    raise SmsDeliveryError(f"短信网关返回异常状态：{response.status}")
        except urllib.error.HTTPError as exc:
            raise SmsDeliveryError(f"短信网关拒绝请求：HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SmsDeliveryError(f"短信网关不可达：{exc}") from exc
        logger.info("验证短信已提交网关：%s", to)


def get_sms_sender(cfg: SmsConfig | None = None) -> SmsSender:
    """按配置返回短信发送器。

    Args:
        cfg: 短信配置；默认取全局配置。

    Returns:
        SmsSender 实例。
    """
    sms_cfg = cfg or get_settings().sms
    if sms_cfg.enabled and sms_cfg.provider == "webhook":
        return WebhookSmsSender(sms_cfg)
    return LogSmsSender()


def is_configured(cfg: SmsConfig | None = None) -> bool:
    """短信通道是否已真正配置。

    Args:
        cfg: 短信配置；默认取全局配置。

    Returns:
        已启用且提供了 webhook 地址时返回 True。
    """
    sms_cfg = cfg or get_settings().sms
    return bool(sms_cfg.enabled and sms_cfg.provider == "webhook" and sms_cfg.webhook_url)


def send_verification_code(*, to: str, code: str, purpose: str, ttl_sec: int) -> None:
    """下发验证码短信。

    Args:
        to: 手机号。
        code: 验证码。
        purpose: 用途。
        ttl_sec: 有效期（秒）。

    Raises:
        SmsDeliveryError: 下发失败。
    """
    get_sms_sender().send(to=to, code=code, purpose=purpose, ttl_sec=ttl_sec)


__all__ = [
    "LogSmsSender",
    "SmsDeliveryError",
    "SmsSender",
    "WebhookSmsSender",
    "get_sms_sender",
    "is_configured",
    "send_verification_code",
]
