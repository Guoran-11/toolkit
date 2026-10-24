"""管理员端点：``/api/admin/users*``。

仅角色为 ``admin`` 的身份可访问（带外超级管理员天然满足）。
自托管工具的用户量很小，因此这里只做必要的分页与搜索，不做复杂 RBAC。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import AdminDep, ClientIpDep
from app.core.errors import ForbiddenError, ValidationError
from app.core.security import write_audit_log
from app.core.users import (
    ROLES,
    STATUSES,
    bump_token_version,
    delete_user,
    get_by_id,
    list_users,
    update_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


class UpdateUserRequest(BaseModel):
    """更新用户请求（字段均可选，只改传入的部分）。"""

    role: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16)
    display_name: str | None = Field(default=None, max_length=64)


@router.get("/api/admin/users")
def list_all_users(
    _admin: AdminDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
    search: Annotated[str, Query(max_length=128)] = "",
) -> dict[str, Any]:
    """分页列出数据库用户。

    Args:
        _admin: 管理员身份。
        page: 页码。
        page_size: 每页条数（前端使用 camelCase）。
        search: 邮箱/显示名模糊搜索。

    Returns:
        ``{"total", "page", "page_size", "items"}``。
    """
    return list_users(page=page, page_size=page_size, search=search)


@router.patch("/api/admin/users/{user_id}")
def update_one_user(
    user_id: int,
    payload: UpdateUserRequest,
    admin: AdminDep,
    ip: ClientIpDep,
) -> dict[str, Any]:
    """修改用户的角色/状态/显示名。

    Args:
        user_id: 目标用户 id。
        payload: 待更新字段。
        admin: 管理员身份。
        ip: 客户端 IP。

    Returns:
        更新后的用户对象。

    Raises:
        ForbiddenError: 试图取消自己的管理员身份。
        ValidationError: 角色或状态非法。
    """
    current_id = admin.get("user_id")
    if payload.role is not None and payload.role not in ROLES:
        raise ValidationError(f"未知角色：{payload.role}")
    if payload.status is not None and payload.status not in STATUSES:
        raise ValidationError(f"未知状态：{payload.status}")

    # 防止管理员把自己降级/禁用，导致无人能管理系统
    if current_id is not None and int(current_id) == int(user_id):
        if payload.role is not None and payload.role != "admin":
            raise ForbiddenError("不能取消自己的管理员身份。")
        if payload.status is not None and payload.status != "active":
            raise ForbiddenError("不能禁用自己的账号。")

    user = update_user(
        user_id,
        role=payload.role,
        status=payload.status,
        display_name=payload.display_name,
    )

    # 禁用账号时立刻吊销其已签发 Token，否则旧 Token 在有效期内仍可访问
    if payload.status == "disabled":
        bump_token_version(user.id)
        user = get_by_id(user.id) or user

    write_audit_log(
        {
            "action": "admin.user_updated",
            "ip": ip,
            "actor": admin.get("sub"),
            "target_user_id": user_id,
            "changes": payload.model_dump(exclude_none=True),
        }
    )
    return user.to_api()


@router.post("/api/admin/users/{user_id}/revoke-tokens")
def revoke_user_tokens(user_id: int, admin: AdminDep, ip: ClientIpDep) -> dict[str, Any]:
    """强制某用户所有已登录会话下线。

    Args:
        user_id: 目标用户 id。
        admin: 管理员身份。
        ip: 客户端 IP。

    Returns:
        ``{"ok": True, "token_version": n}``。
    """
    version = bump_token_version(user_id)
    write_audit_log(
        {"action": "admin.revoke_tokens", "ip": ip, "actor": admin.get("sub"), "target_user_id": user_id}
    )
    return {"ok": True, "token_version": version}


@router.delete("/api/admin/users/{user_id}")
def delete_one_user(user_id: int, admin: AdminDep, ip: ClientIpDep) -> dict[str, Any]:
    """删除用户。

    Args:
        user_id: 目标用户 id。
        admin: 管理员身份。
        ip: 客户端 IP。

    Returns:
        ``{"ok": True, "removed": bool}``。

    Raises:
        ForbiddenError: 试图删除自己。
    """
    if admin.get("user_id") is not None and int(admin["user_id"]) == int(user_id):
        raise ForbiddenError("不能删除自己的账号。")

    removed = delete_user(user_id)
    write_audit_log(
        {"action": "admin.user_deleted", "ip": ip, "actor": admin.get("sub"), "target_user_id": user_id}
    )
    return {"ok": True, "removed": removed}


__all__ = ["router"]
