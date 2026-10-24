"""FastAPI 依赖注入。

集中定义跨路由复用的依赖：
    - ``get_config``   —— 取全局配置
    - ``optional_user``—— 解析登录态（不强制）
    - ``guard``        —— 按配置强制鉴权（业务路由统一挂载）
    - ``client_ip``    —— 取真实客户端 IP（用于 SSH 白名单与审计）
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import token_manager

# Bearer Token 的请求头解析：前端 api.js 发送 "Authorization: Bearer <token>"
_AUTH_HEADER = "authorization"


def get_config() -> Settings:
    """取全局配置。

    Returns:
        Settings 实例。
    """
    return get_settings()


ConfigDep = Annotated[Settings, Depends(get_config)]


def _extract_token(request: Request) -> str | None:
    """从请求中提取 Token。

    依次尝试 Authorization 头与查询参数（后者用于 ``<audio src>``、
    ``<iframe src>`` 这类无法自定义请求头的原生消费场景）。

    Args:
        request: 请求对象。

    Returns:
        Token 字符串；未提供返回 None。
    """
    header = request.headers.get(_AUTH_HEADER, "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    return None


def _resolve_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """把 Token 载荷解析为有效身份（含数据库用户的实时状态校验）。

    Token 是无状态的，但数据库用户可能被改密、禁用或删除。因此对带 ``uid``
    的 Token 必须回查数据库：

        · 用户不存在 / 已禁用 → 身份失效
        · ``tv`` 与库中 ``token_version`` 不一致 → 身份失效（改密后强制下线）

    这一层是「可吊销」能力的落点——在此之前，项目里的 Token 一旦签发就
    在 24 小时内无法作废。

    Args:
        payload: 已通过签名校验的 Token 载荷。

    Returns:
        有效身份字典；失效返回 None。
    """
    user_id = payload.get("uid")
    if user_id is None:
        # config.toml 里的带外超级管理员：不落库，无需回查
        return payload

    from app.core.users import get_by_id

    user = get_by_id(int(user_id))
    if user is None or not user.is_active:
        return None
    if int(payload.get("tv", -1)) != user.token_version:
        return None

    resolved = dict(payload)
    resolved["sub"] = user.email
    resolved["role"] = user.role
    resolved["display_name"] = user.display_name or user.email
    resolved["user_id"] = user.id
    return resolved


def optional_user(request: Request, settings: ConfigDep) -> dict[str, Any] | None:
    """解析登录态，缺失或失效时返回 None（不抛错）。

    Args:
        request: 请求对象。
        settings: 配置。

    Returns:
        用户载荷字典，或 None（未登录 / 未启用鉴权）。
    """
    if not settings.server.auth_required:
        # 离线单机版：鉴权关闭，视为本机管理员
        return {"sub": "local", "role": "admin", "local": True}

    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = token_manager.verify(token)
    except UnauthorizedError:
        return None
    return _resolve_identity(payload)


OptionalUserDep = Annotated[dict[str, Any] | None, Depends(optional_user)]


def guard(user: OptionalUserDep, settings: ConfigDep) -> dict[str, Any]:
    """业务路由的强制鉴权依赖。

    服务器版（``auth_required=true``）下所有业务接口都必须携带有效 Token；
    离线单机版直接放行并返回本机管理员身份。

    Args:
        user: 解析出的登录态。
        settings: 配置。

    Returns:
        用户载荷字典。

    Raises:
        UnauthorizedError: 需要鉴权但未登录/已过期。
    """
    if user is None:
        raise UnauthorizedError("登录状态已失效，请重新登录")
    return user


GuardDep = Annotated[dict[str, Any], Depends(guard)]


def require_admin(user: GuardDep) -> dict[str, Any]:
    """管理员专用依赖（用户管理端点使用）。

    Args:
        user: 已解析的登录态。

    Returns:
        管理员身份。

    Raises:
        ForbiddenError: 当前身份不是管理员。
    """
    if user.get("role") != "admin":
        raise ForbiddenError("需要管理员权限")
    return user


AdminDep = Annotated[dict[str, Any], Depends(require_admin)]


def _is_trusted_proxy(peer: str, trusted: list[str]) -> bool:
    """判断直连对端是否为可信反向代理。

    Args:
        peer: 直连地址（``request.client.host``）。
        trusted: 可信代理列表，元素为 IP 或 CIDR。

    Returns:
        可信返回 True；无法解析的地址一律视为不可信（fail closed）。

    说明：回环地址始终可信，覆盖「Nginx 与应用同机」这一默认拓扑。
    """
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False

    if peer_ip.is_loopback:
        return True

    for entry in trusted:
        try:
            if "/" in entry:
                if peer_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer_ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """取客户端真实 IP。

    ``X-Forwarded-For`` / ``X-Real-IP`` 是**客户端可伪造**的请求头，因此仅在
    直连对端是可信代理时才采信（见 ``security.trusted_proxies``，回环默认可信）。
    否则一律使用直连地址。

    这一点直接影响安全：本函数的结果用于登录失败限流、审计日志与 SSH 白名单判定，
    无条件信任转发头等于让攻击者随意伪造来源 IP 来绕过爆破防护。

    Args:
        request: 请求对象。

    Returns:
        IP 字符串；无法获取时返回 ``unknown``。
    """
    peer = request.client.host if request.client else ""

    if peer and _is_trusted_proxy(peer, get_settings().security.trusted_proxies):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # 取首段：它是离客户端最近的一跳（后续为代理链）
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
        real_ip = request.headers.get("x-real-ip")
        if real_ip and real_ip.strip():
            return real_ip.strip()

    return peer or "unknown"


ClientIpDep = Annotated[str, Depends(client_ip)]


__all__ = [
    "AdminDep",
    "ClientIpDep",
    "ConfigDep",
    "GuardDep",
    "OptionalUserDep",
    "client_ip",
    "get_config",
    "guard",
    "optional_user",
    "require_admin",
]
