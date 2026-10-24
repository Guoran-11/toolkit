"""用户仓储：账号的增删改查与状态流转。

与 ``config.toml`` 中「超级管理员」的关系：
    · ``config.toml`` 的 ``[server] username/password_hash`` 是**带外超级管理员**，
      不落库、不受本模块影响。它的价值在于救援：数据库损坏、注册系统故障时
      仍然能登录进去修。
    · 本模块管理的是**数据库用户**（邮箱注册产生），角色为 ``user`` 或 ``admin``。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.db import get_connection, transaction
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import hash_password, verify_password
from app.core.tasks import utc_now_iso

# 邮箱格式：刻意保守（不追求 RFC 5322 全量），够用且能挡住明显垃圾输入
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 允许的角色与状态
ROLES: frozenset[str] = frozenset({"user", "admin"})
STATUSES: frozenset[str] = frozenset({"active", "disabled"})

# 显示名长度上限
_MAX_NAME_LEN = 64


def normalize_email(email: str) -> str:
    """规范化邮箱（去空白 + 转小写）。

    Args:
        email: 原始邮箱。

    Returns:
        规范化后的邮箱。

    Raises:
        ValidationError: 格式不合法。
    """
    value = (email or "").strip().lower()
    if not value or len(value) > 254 or not _EMAIL_RE.match(value):
        raise ValidationError("邮箱格式不正确")
    return value


@dataclass(slots=True)
class User:
    """数据库用户。"""

    id: int
    email: str
    phone: str | None
    display_name: str
    password_hash: str
    role: str
    status: str
    email_verified: bool
    token_version: int
    created_at: str
    last_login_at: str | None

    @property
    def is_active(self) -> bool:
        """账号是否可用。"""
        return self.status == "active"

    def to_api(self) -> dict[str, Any]:
        """转换为对外结构（**绝不包含 password_hash**）。

        Returns:
            前端可安全消费的用户字典。
        """
        return {
            "id": self.id,
            "email": self.email,
            "username": self.email,
            "display_name": self.display_name or self.email.split("@")[0],
            "phone": self.phone,
            "role": self.role,
            "status": self.status,
            "email_verified": self.email_verified,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


def _row_to_user(row: sqlite3.Row) -> User:
    """把数据库行转成 User。

    Args:
        row: ``users`` 表的一行。

    Returns:
        User 实例。
    """
    return User(
        id=int(row["id"]),
        email=str(row["email"]),
        phone=row["phone"],
        display_name=str(row["display_name"] or ""),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        status=str(row["status"]),
        email_verified=bool(row["email_verified"]),
        token_version=int(row["token_version"]),
        created_at=str(row["created_at"]),
        last_login_at=row["last_login_at"],
    )


def create_user(
    *,
    email: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    status: str = "active",
    email_verified: bool = True,
    phone: str | None = None,
) -> User:
    """创建用户。

    Args:
        email: 邮箱（会规范化）。
        password: 明文密码（仅在此处哈希，不落明文）。
        display_name: 显示名。
        role: 角色。
        status: 状态。
        email_verified: 邮箱是否已验证。
        phone: 手机号（可选）。

    Returns:
        新建的 User。

    Raises:
        ValidationError: 参数非法。
        ConflictError: 邮箱已注册。
    """
    normalized = normalize_email(email)
    if role not in ROLES:
        raise ValidationError(f"未知角色：{role}")
    if status not in STATUSES:
        raise ValidationError(f"未知状态：{status}")

    name = (display_name or "").strip()[:_MAX_NAME_LEN]
    digest = hash_password(password)
    now = utc_now_iso()

    try:
        with transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (email, phone, display_name, password_hash, role, status,
                                   email_verified, token_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (normalized, phone, name, digest, role, status, 1 if email_verified else 0, now),
            )
            user_id = int(cursor.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        # 唯一索引冲突：邮箱或手机号已存在
        if "phone" in str(exc):
            raise ConflictError("该手机号已被注册") from exc
        raise ConflictError("该邮箱已被注册") from exc

    user = get_by_id(user_id)
    if user is None:  # pragma: no cover - 插入成功后必然可查到
        raise NotFoundError("用户创建失败")
    return user


def get_by_email(email: str) -> User | None:
    """按邮箱查用户。

    Args:
        email: 邮箱。

    Returns:
        User 或 None（邮箱格式非法时也返回 None，不抛错，避免成为枚举接口）。
    """
    value = (email or "").strip().lower()
    if not value:
        return None
    row = get_connection().execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (value,)
    ).fetchone()
    return _row_to_user(row) if row else None


def get_by_id(user_id: int) -> User | None:
    """按主键查用户。

    Args:
        user_id: 用户 id。

    Returns:
        User 或 None。
    """
    row = get_connection().execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(email: str, password: str) -> User | None:
    """校验邮箱 + 密码。

    Args:
        email: 邮箱。
        password: 明文密码。

    Returns:
        校验通过且账号可用时返回 User；否则返回 None（不区分「不存在」与「密码错」）。
    """
    user = get_by_email(email)
    if user is None:
        # 即使用户不存在也做一次哈希运算，避免通过响应耗时区分账号是否存在
        verify_password(password, "pbkdf2_sha256$260000$" + "0" * 32 + "$" + "0" * 64)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def set_password(user_id: int, new_password: str, *, bump_version: bool = True) -> None:
    """更新密码。

    Args:
        user_id: 用户 id。
        new_password: 新明文密码。
        bump_version: 是否同时递增 token_version（改密后让旧 Token 立即失效）。
    """
    digest = hash_password(new_password)
    with transaction() as conn:
        if bump_version:
            conn.execute(
                "UPDATE users SET password_hash = ?, token_version = token_version + 1 WHERE id = ?",
                (digest, int(user_id)),
            )
        else:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (digest, int(user_id)))


def mark_login(user_id: int) -> None:
    """记录最近登录时间。

    Args:
        user_id: 用户 id。
    """
    with transaction() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utc_now_iso(), int(user_id)))


def bump_token_version(user_id: int) -> int:
    """递增 token_version（强制该用户所有已签发 Token 失效）。

    Args:
        user_id: 用户 id。

    Returns:
        新的 token_version。
    """
    with transaction() as conn:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (int(user_id),))
        row = conn.execute("SELECT token_version FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return int(row["token_version"]) if row else 1


def update_user(
    user_id: int,
    *,
    role: str | None = None,
    status: str | None = None,
    display_name: str | None = None,
) -> User:
    """更新用户属性（管理员操作）。

    Args:
        user_id: 用户 id。
        role: 新角色。
        status: 新状态。
        display_name: 新显示名。

    Returns:
        更新后的 User。

    Raises:
        NotFoundError: 用户不存在。
        ValidationError: 角色或状态非法。
    """
    fields: list[str] = []
    values: list[Any] = []

    if role is not None:
        if role not in ROLES:
            raise ValidationError(f"未知角色：{role}")
        fields.append("role = ?")
        values.append(role)
    if status is not None:
        if status not in STATUSES:
            raise ValidationError(f"未知状态：{status}")
        fields.append("status = ?")
        values.append(status)
    if display_name is not None:
        fields.append("display_name = ?")
        values.append(display_name.strip()[:_MAX_NAME_LEN])

    if fields:
        values.append(int(user_id))
        with transaction() as conn:
            cursor = conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
            if cursor.rowcount == 0:
                raise NotFoundError("用户不存在")

    user = get_by_id(user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    return user


def delete_user(user_id: int) -> bool:
    """删除用户。

    Args:
        user_id: 用户 id。

    Returns:
        是否确实删除。
    """
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
    return cursor.rowcount > 0


def list_users(*, page: int = 1, page_size: int = 50, search: str = "") -> dict[str, Any]:
    """分页列出用户。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数。
        search: 邮箱/显示名模糊匹配。

    Returns:
        ``{"total", "page", "page_size", "items"}``。
    """
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    where = ""
    params: list[Any] = []
    if search.strip():
        where = " WHERE email LIKE ? OR display_name LIKE ?"
        like = f"%{search.strip()}%"
        params = [like, like]

    conn = get_connection()
    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM users{where}", params).fetchone()["n"])
    rows = conn.execute(
        f"SELECT * FROM users{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_row_to_user(row).to_api() for row in rows],
    }


def count_users() -> int:
    """用户总数（用于健康检查与首次初始化判断）。

    Returns:
        用户数量。
    """
    return int(get_connection().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


__all__ = [
    "ROLES",
    "STATUSES",
    "User",
    "authenticate",
    "bump_token_version",
    "count_users",
    "create_user",
    "delete_user",
    "get_by_email",
    "get_by_id",
    "list_users",
    "mark_login",
    "normalize_email",
    "set_password",
    "update_user",
]
