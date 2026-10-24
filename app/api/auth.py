"""鉴权端点：``/api/auth/*``。

账号体系有两种身份，共存且互不干扰：

    1. **带外超级管理员** —— ``config.toml`` 的 ``[server] username/password_hash``。
       不落库、不受数据库影响。它的价值在于**救援**：数据库损坏、注册系统故障、
       唯一管理员被误删时，仍然能登录进去修。
    2. **数据库用户** —— 邮箱注册产生，角色 ``user`` / ``admin``，存在 SQLite 中。

端点一览：

    ================================  ==========================================
    GET  /api/auth/config             公开：前端据此渲染注册/找回入口
    POST /api/auth/send-code          发送邮箱/短信验证码
    POST /api/auth/register           邮箱 + 验证码注册
    POST /api/auth/login              登录（超级管理员 或 数据库用户）
    POST /api/auth/logout             登出（审计留痕）
    GET  /api/auth/me                 当前身份
    POST /api/auth/password           修改密码
    POST /api/auth/reset-password     忘记密码（邮箱验证码重置）
    ================================  ==========================================

**登录限流**：进程内按 IP 的滑动窗口失败计数。服务器版一旦暴露在公网，
默认口令 + 无限重试就是最高危入口（风险 R6）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ClientIpDep, ConfigDep, GuardDep
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
    ValidationError,
)
from app.core.mailer import is_configured as email_configured
from app.core.security import (
    bootstrap_password,
    hash_password,
    token_manager,
    verify_password,
    write_audit_log,
)
from app.core.sms import is_configured as sms_configured
from app.core.users import (
    authenticate,
    create_user,
    get_by_email,
    get_by_id,
    mark_login,
    normalize_email,
    set_password,
)
from app.core.verification import consume as consume_code
from app.core.verification import issue as issue_code

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# 登录限流参数：窗口内允许的最大失败次数
_MAX_FAILURES: int = 8
_FAILURE_WINDOW_SEC: int = 300

# 弱口令黑名单（本地自托管场景够用；真正的强度靠长度 + 唯一性）
_WEAK_PASSWORDS: frozenset[str] = frozenset(
    {"12345678", "123456789", "password", "passw0rd", "qwertyui", "admin123", "11111111"}
)


# ---------------------------------------------------------------- 请求模型


class LoginRequest(BaseModel):
    """登录请求。

    Attributes:
        username: 用户名（数据库用户填邮箱）。
        password: 密码。
        code: 邮箱验证码（仅当开启二次验证时需要）。
    """

    username: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=512)
    code: str = Field(default="", max_length=16)


class ChangePasswordRequest(BaseModel):
    """修改密码请求。"""

    old_password: str = Field(..., max_length=512)
    # 长度下限交由 _validate_password 统一判定（唯一事实来源来自 config.toml）
    new_password: str = Field(..., max_length=512)


class SendCodeRequest(BaseModel):
    """发送验证码请求。"""

    target: str = Field(..., max_length=254)
    channel: str = Field(default="email", max_length=16)
    purpose: str = Field(default="register", max_length=16)


class RegisterRequest(BaseModel):
    """注册请求。"""

    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=512)
    code: str = Field(default="", max_length=16)
    display_name: str = Field(default="", max_length=64)


class ResetPasswordRequest(BaseModel):
    """忘记密码请求。"""

    email: str = Field(..., max_length=254)
    code: str = Field(..., max_length=16)
    new_password: str = Field(..., max_length=512)


# ---------------------------------------------------------------- 限流


class _LoginThrottle:
    """进程内登录失败限流器。

    按来源 IP 记录失败次数与时间戳。使用滑动窗口而非固定窗口，
    避免攻击者在窗口边界处获得双倍尝试次数。
    """

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.RLock()
        self._failures: dict[str, list[float]] = {}

    def check(self, ip: str) -> None:
        """检查该 IP 是否处于封禁状态。

        Args:
            ip: 客户端 IP。

        Raises:
            ForbiddenError: 失败次数超限。
        """
        now = time.time()
        with self._lock:
            stamps = [t for t in self._failures.get(ip, []) if now - t < _FAILURE_WINDOW_SEC]
            self._failures[ip] = stamps
            if len(stamps) >= _MAX_FAILURES:
                wait = int(_FAILURE_WINDOW_SEC - (now - stamps[0]))
                raise ForbiddenError(f"登录失败次数过多，请在 {max(1, wait)} 秒后重试。")

    def record_failure(self, ip: str) -> None:
        """记录一次失败。

        Args:
            ip: 客户端 IP。
        """
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        """登录成功后清空该 IP 的失败记录。

        Args:
            ip: 客户端 IP。
        """
        with self._lock:
            self._failures.pop(ip, None)


_throttle = _LoginThrottle()


# ---------------------------------------------------------------- 内部工具


def _validate_password(password: str, *, email: str = "") -> None:
    """校验密码强度。

    Args:
        password: 明文密码。
        email: 关联邮箱（用于拒绝「密码就是邮箱前缀」这类弱口令）。

    Raises:
        ValidationError: 不符合要求。
    """
    settings = get_settings()
    min_len = settings.auth.min_password_length

    if not password or password.strip() == "":
        raise ValidationError("密码不能为空。")
    if len(password) < min_len:
        raise ValidationError(f"密码长度至少 {min_len} 位。")
    if password.lower() in _WEAK_PASSWORDS:
        raise ValidationError("该密码过于常见，请更换更复杂的密码。")
    if len(set(password)) < 4:
        raise ValidationError("密码字符过于单一，请包含更多种类的字符。")
    if email:
        local = email.split("@")[0].lower()
        # 只拒绝「密码就是邮箱账号名」这一真正弱的情形。
        # 早先写成「账号名是密码的子串」会把 AlicePass-2026 这类正常密码也拒掉。
        if local and password.lower() == local:
            raise ValidationError("密码不能与邮箱账号名相同。")


def _superadmin_username() -> str:
    """取 config.toml 中的超级管理员用户名。

    Returns:
        用户名。
    """
    return get_settings().server.username


def _try_superadmin(username: str, password: str) -> dict[str, Any] | None:
    """尝试以带外超级管理员身份登录。

    Args:
        username: 提交的用户名。
        password: 提交的密码。

    Returns:
        成功时返回签发的登录结果；用户名不匹配返回 None。

    Raises:
        UnauthorizedError: 用户名匹配但密码错误。
    """
    settings = get_settings()
    if username != settings.server.username:
        return None

    # 首次启动或未配置密码：引导初始化
    if not settings.server.password_hash:
        generated = bootstrap_password()
        if generated:
            # 明文只打印到控制台；日志会长期留存，绝不写入
            logger.warning("未配置管理员密码，已自动生成初始密码并在控制台打印，请登录后立即修改。")

    if not verify_password(password, settings.server.password_hash):
        raise UnauthorizedError("用户名或密码不正确")

    return token_manager.issue(username, role="admin", display_name="超级管理员")


def _login_db_user(user_row: Any, *, require_code: bool, code: str, ip: str) -> dict[str, Any]:
    """为数据库用户签发登录结果。

    Args:
        user_row: ``users.User`` 实例。
        require_code: 是否强制校验邮箱验证码。
        code: 提交的验证码。
        ip: 客户端 IP。

    Returns:
        登录结果字典。

    Raises:
        ForbiddenError: 账号被禁用。
        ValidationError: 验证码缺失或不正确。
    """
    if not user_row.is_active:
        raise ForbiddenError("该账号已被禁用，请联系管理员。")

    if require_code:
        if not code:
            raise ValidationError("请输入邮箱验证码。")
        consume_code(target=user_row.email, purpose="login", code=code)

    mark_login(user_row.id)
    return token_manager.issue(
        user_row.email,
        role=user_row.role,
        user_id=user_row.id,
        token_version=user_row.token_version,
        display_name=user_row.display_name or user_row.email,
    )


# ---------------------------------------------------------------- 公开配置


@router.get("/api/auth/config")
def auth_config(settings: ConfigDep) -> dict[str, Any]:
    """公开的鉴权配置（**无需登录**）。

    前端据此决定是否显示「注册」「忘记密码」入口，以及提示「邮件服务未配置」。

    Args:
        settings: 全局配置。

    Returns:
        鉴权相关的前端可用配置。
    """
    return {
        "auth_required": settings.server.auth_required,
        "allow_registration": settings.auth.allow_registration,
        "require_email_verification": settings.auth.require_email_verification,
        "login_require_code": settings.auth.login_require_code,
        "min_password_length": settings.auth.min_password_length,
        "code_ttl_sec": settings.auth.code_ttl_sec,
        "code_resend_sec": settings.auth.code_resend_sec,
        "email_configured": email_configured(settings.email),
        "sms_configured": sms_configured(settings.sms),
    }


# ---------------------------------------------------------------- 验证码


@router.post("/api/auth/send-code")
def send_code(payload: SendCodeRequest, settings: ConfigDep, ip: ClientIpDep) -> dict[str, Any]:
    """发送验证码。

    Args:
        payload: 目标、通道与用途。
        settings: 全局配置。
        ip: 客户端 IP（用于限流）。

    Returns:
        ``{"ok": True, "expires_in": 300, "resend_after": 60, "delivered": bool}``。

    Raises:
        ForbiddenError: 注册未开放（``purpose=register`` 时）。
        ConflictError: 注册时邮箱已存在。
        ValidationError: 参数非法。
        RateLimitedError: 触发限流。
        MailDeliveryError / SmsDeliveryError: 下发失败。
    """
    purpose = (payload.purpose or "register").strip().lower()
    channel = (payload.channel or "email").strip().lower()
    target = (payload.target or "").strip()

    if purpose == "register":
        if not settings.auth.allow_registration:
            raise ForbiddenError("管理员尚未开放注册。")
        target = normalize_email(target)
        if get_by_email(target) is not None:
            raise ConflictError("该邮箱已被注册，请直接登录或使用「忘记密码」。")
    elif purpose == "reset":
        target = normalize_email(target)
        # 防账号枚举：邮箱未注册时同样返回成功，但不实际发码。
        # 由于没有签发验证码，后续 reset-password 必然校验失败。
        if get_by_email(target) is None:
            logger.info("重置密码请求的邮箱未注册，已静默忽略：%s", target)
            return {
                "ok": True,
                "expires_in": settings.auth.code_ttl_sec,
                "resend_after": settings.auth.code_resend_sec,
                "delivered": False,
            }
    elif purpose == "login":
        target = normalize_email(target)
        user = get_by_email(target)
        if user is None or not user.is_active:
            raise ValidationError("该邮箱不可用。")
    else:
        raise ValidationError(f"不支持的验证码用途：{purpose}")

    issue_code(target=target, channel=channel, purpose=purpose, ip=ip)  # type: ignore[arg-type]
    write_audit_log({"action": "auth.send_code", "ip": ip, "target": target, "purpose": purpose})

    return {
        "ok": True,
        "expires_in": settings.auth.code_ttl_sec,
        "resend_after": settings.auth.code_resend_sec,
        "delivered": True,
    }


# ---------------------------------------------------------------- 注册


@router.post("/api/auth/register")
def register(payload: RegisterRequest, settings: ConfigDep, ip: ClientIpDep) -> dict[str, Any]:
    """邮箱注册。

    Args:
        payload: 邮箱、密码、验证码与显示名。
        settings: 全局配置。
        ip: 客户端 IP。

    Returns:
        注册成功后的登录结果（含 Token），用户无需再登录一次。

    Raises:
        ForbiddenError: 注册未开放。
        ConflictError: 邮箱已注册。
        ValidationError: 验证码或密码不合法。
    """
    if not settings.auth.allow_registration:
        raise ForbiddenError("管理员尚未开放注册。")

    email = normalize_email(payload.email)
    if get_by_email(email) is not None:
        raise ConflictError("该邮箱已被注册，请直接登录。")

    if settings.auth.require_email_verification:
        if not (payload.code or "").strip():
            raise ValidationError("请输入邮箱验证码。")
        consume_code(target=email, purpose="register", code=payload.code)

    _validate_password(payload.password, email=email)

    user = create_user(
        email=email,
        password=payload.password,
        display_name=payload.display_name,
        role=settings.auth.default_role,
        status=settings.auth.default_status,
        email_verified=bool(settings.auth.require_email_verification),
    )
    write_audit_log({"action": "auth.register", "ip": ip, "email": user.email, "user_id": user.id})

    if not user.is_active:
        # 需管理员审批的场景：注册成功但暂不可登录
        return {"ok": True, "pending_approval": True, "user": user.to_api()}

    return _login_db_user(user, require_code=False, code="", ip=ip)


# ---------------------------------------------------------------- 登录 / 登出


@router.post("/api/auth/login")
def login(payload: LoginRequest, settings: ConfigDep, ip: ClientIpDep) -> dict[str, Any]:
    """登录并签发 Token。

    先尝试带外超级管理员，再尝试数据库用户。

    Args:
        payload: 登录凭据（可选验证码）。
        settings: 全局配置。
        ip: 客户端 IP。

    Returns:
        ``{"token": ..., "user": {...}, "expires_in": ...}``。

    Raises:
        ValidationError: 用户名或密码为空。
        ForbiddenError: 失败次数超限 / 账号被禁用。
        UnauthorizedError: 凭据不正确。
    """
    username = (payload.username or "").strip()
    password = payload.password or ""

    if not username or not password:
        raise ValidationError("用户名与密码不能为空")

    _throttle.check(ip)

    # ---- 1) 带外超级管理员 ----
    try:
        superadmin = _try_superadmin(username, password)
    except UnauthorizedError:
        _throttle.record_failure(ip)
        write_audit_log({"action": "auth.login.failed", "ip": ip, "username": username})
        raise

    if superadmin is not None:
        _throttle.reset(ip)
        write_audit_log({"action": "auth.login", "ip": ip, "username": username, "role": "superadmin"})
        return superadmin

    # ---- 2) 数据库用户 ----
    user = authenticate(username, password)
    if user is None:
        _throttle.record_failure(ip)
        write_audit_log({"action": "auth.login.failed", "ip": ip, "username": username})
        # 不区分「用户名错」与「密码错」：避免成为用户名枚举接口
        raise UnauthorizedError("用户名或密码不正确")

    try:
        result = _login_db_user(
            user,
            require_code=settings.auth.login_require_code,
            code=payload.code,
            ip=ip,
        )
    except (ForbiddenError, ValidationError):
        _throttle.record_failure(ip)
        write_audit_log({"action": "auth.login.failed", "ip": ip, "username": username})
        raise

    _throttle.reset(ip)
    write_audit_log({"action": "auth.login", "ip": ip, "username": user.email, "role": user.role})
    return result


@router.post("/api/auth/logout")
def logout(ip: ClientIpDep) -> dict[str, bool]:
    """登出（服务端无状态 Token，此处主要用于审计留痕）。

    Args:
        ip: 客户端 IP。

    Returns:
        ``{"ok": True}``。
    """
    write_audit_log({"action": "auth.logout", "ip": ip})
    return {"ok": True}


@router.get("/api/auth/me")
def whoami(_user: GuardDep) -> dict[str, Any]:
    """返回当前登录身份。

    Args:
        _user: 鉴权主体。

    Returns:
        用户信息与外层的鉴权配置。
    """
    settings = get_settings()
    return {
        "user": {
            "username": _user.get("sub"),
            "email": _user.get("sub"),
            "display_name": _user.get("display_name") or _user.get("sub"),
            "role": _user.get("role", "admin"),
            "user_id": _user.get("user_id"),
        },
        "auth_required": settings.server.auth_required,
        "local": bool(_user.get("local")),
        "is_superadmin": _user.get("user_id") is None,
    }


# ---------------------------------------------------------------- 密码


@router.post("/api/auth/password")
def change_password(
    payload: ChangePasswordRequest,
    _user: GuardDep,
    ip: ClientIpDep,
) -> dict[str, Any]:
    """修改密码。

    数据库用户改密后会递增 ``token_version``（旧 Token 全部失效），
    因此这里返回**新签发的 Token**，让当前会话平滑延续。

    Args:
        payload: 旧密码与新密码。
        _user: 鉴权主体。
        ip: 客户端 IP。

    Returns:
        ``{"ok": True, "token": <新 Token>}``（超级管理员不返回 token）。

    Raises:
        UnauthorizedError: 旧密码不正确。
        ValidationError: 新密码不符合要求。
    """
    settings = get_settings()
    user_id = _user.get("user_id")

    if user_id is None:
        # ---- 带外超级管理员 ----
        if not verify_password(payload.old_password, settings.server.password_hash):
            _throttle.record_failure(ip)
            raise UnauthorizedError("当前密码不正确")
        if payload.new_password == payload.old_password:
            raise ValidationError("新密码不能与当前密码相同。")
        _validate_password(payload.new_password)

        from app.core.security import _persist_password_hash

        settings.server.password_hash = hash_password(payload.new_password)
        _persist_password_hash(settings.server.password_hash)
        write_audit_log({"action": "auth.password_changed", "ip": ip, "scope": "superadmin"})
        return {"ok": True, "token": None}

    # ---- 数据库用户 ----
    user = get_by_id(int(user_id))
    if user is None:
        raise UnauthorizedError("登录状态已失效，请重新登录")
    if not verify_password(payload.old_password, user.password_hash):
        _throttle.record_failure(ip)
        raise UnauthorizedError("当前密码不正确")
    if payload.new_password == payload.old_password:
        raise ValidationError("新密码不能与当前密码相同。")
    _validate_password(payload.new_password, email=user.email)

    set_password(user.id, payload.new_password, bump_version=True)
    refreshed = get_by_id(user.id)
    write_audit_log({"action": "auth.password_changed", "ip": ip, "scope": "user", "user_id": user.id})

    if refreshed is None:  # pragma: no cover - 刚更新过必然存在
        return {"ok": True, "token": None}
    issued = token_manager.issue(
        refreshed.email,
        role=refreshed.role,
        user_id=refreshed.id,
        token_version=refreshed.token_version,
        display_name=refreshed.display_name or refreshed.email,
    )
    return {"ok": True, "token": issued["token"], "user": issued["user"], "expires_in": issued["expires_in"]}


@router.post("/api/auth/reset-password")
def reset_password(payload: ResetPasswordRequest, settings: ConfigDep, ip: ClientIpDep) -> dict[str, Any]:
    """忘记密码：用邮箱验证码重置。

    超级管理员的口令在 ``config.toml`` 里，**不走本端点**（改不了文件系统权限
    之外的东西，也不该让网络请求改写带外凭据）。超级管理员请用
    ``scripts/reset_admin.py``。

    Args:
        payload: 邮箱、验证码与新密码。
        settings: 全局配置。
        ip: 客户端 IP。

    Returns:
        ``{"ok": True}``。

    Raises:
        ValidationError: 验证码不正确或新密码不合规。
    """
    email = normalize_email(payload.email)
    user = get_by_email(email)

    if user is None:
        # 防枚举：与「验证码错误」返回同一类提示
        raise ValidationError("验证码无效或已过期，请重新获取。")

    consume_code(target=email, purpose="reset", code=payload.code)
    _validate_password(payload.new_password, email=email)

    set_password(user.id, payload.new_password, bump_version=True)
    write_audit_log({"action": "auth.password_reset", "ip": ip, "user_id": user.id})
    return {"ok": True}


__all__ = ["router"]
