#!/usr/bin/env python3
"""带外管理员救援工具（不依赖邮箱 / 短信 / 网络）。

用途：忘记超级管理员密码、或需要在不登录的情况下管理账号时，在服务器上直接执行。

用法::

    # 生成随机新密码并写回 config.toml（明文只打印一次）
    python scripts/reset_admin.py

    # 指定新密码
    python scripts/reset_admin.py --password 'MyNewPass123'

    # 查看数据库里的注册用户
    python scripts/reset_admin.py --list-users

    # 把某个注册用户提升为管理员
    python scripts/reset_admin.py --promote someone@example.com

    # 强制某用户下线（吊销其所有已签发 Token）
    python scripts/reset_admin.py --revoke someone@example.com

设计说明：
    超级管理员的口令存在 ``config.toml``（PBKDF2 哈希），**不在数据库里**。
    这样即使 ``toolkit.db`` 损坏、注册系统故障、唯一管理员被误删，
    仍然有一条确定的、不依赖任何外部服务的进站通道。本脚本就是这条通道的入口。
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

# 允许以 `python scripts/reset_admin.py` 直接运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_console_safe() -> None:
    """让输出在任意控制台编码下都不会崩。

    Windows 中文控制台默认是 GBK，输出某些符号会抛 ``UnicodeEncodeError``。
    这里把不可编码字符替换掉，而不是让脚本失败 —— 救援工具在任何环境下
    都必须能跑完。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _reset_superadmin(password: str | None) -> int:
    """重置超级管理员密码。

    Args:
        password: 指定的新密码；None 表示随机生成。

    Returns:
        进程退出码。
    """
    from app.core.config import CONFIG_FILE, get_settings
    from app.core.security import _persist_password_hash, hash_password

    settings = get_settings()
    new_password = password or secrets.token_urlsafe(12)

    if len(new_password) < 8:
        print("[ERROR] 密码长度至少 8 位。", file=sys.stderr)
        return 2

    settings.server.password_hash = hash_password(new_password)
    _persist_password_hash(settings.server.password_hash)

    print("=" * 60)
    print("  超级管理员密码已重置")
    print(f"  用户名：{settings.server.username}")
    print(f"  新密码：{new_password}")
    print(f"  已写入：{CONFIG_FILE}")
    print("  请立即登录并在「设置」中修改；此密码不会再次显示。")
    print("=" * 60)
    return 0


def _list_users() -> int:
    """列出数据库用户。

    Returns:
        进程退出码。
    """
    from app.core.db import init_db
    from app.core.users import list_users

    init_db()
    data = list_users(page=1, page_size=200)
    print(f"共 {data['total']} 个注册用户：")
    print(f"{'ID':<6}{'邮箱':<34}{'角色':<8}{'状态':<10}{'最近登录'}")
    print("-" * 84)
    for item in data["items"]:
        print(
            f"{item['id']:<6}{item['email']:<34}{item['role']:<8}"
            f"{item['status']:<10}{item.get('last_login_at') or '-'}"
        )
    return 0


def _find_user(identifier: str):
    """按邮箱或 id 查用户。

    Args:
        identifier: 邮箱或纯数字 id。

    Returns:
        User 或 None。
    """
    from app.core.users import get_by_email, get_by_id

    if identifier.isdigit():
        return get_by_id(int(identifier))
    return get_by_email(identifier)


def _promote(identifier: str) -> int:
    """把用户提升为管理员。

    Args:
        identifier: 邮箱或 id。

    Returns:
        进程退出码。
    """
    from app.core.db import init_db
    from app.core.users import update_user

    init_db()
    user = _find_user(identifier)
    if user is None:
        print(f"[ERROR] 未找到用户：{identifier}", file=sys.stderr)
        return 1
    update_user(user.id, role="admin")
    print(f"[OK] 已将 {user.email} 提升为管理员。")
    return 0


def _revoke(identifier: str) -> int:
    """吊销用户的所有 Token（强制下线）。

    Args:
        identifier: 邮箱或 id。

    Returns:
        进程退出码。
    """
    from app.core.db import init_db
    from app.core.users import bump_token_version

    init_db()
    user = _find_user(identifier)
    if user is None:
        print(f"[ERROR] 未找到用户：{identifier}", file=sys.stderr)
        return 1
    version = bump_token_version(user.id)
    print(f"[OK] 已吊销 {user.email} 的全部登录态（token_version={version}）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        prog="reset_admin",
        description="全能工具站 —— 带外管理员救援工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/reset_admin.py\n"
            "  python scripts/reset_admin.py --password 'MyNewPass123'\n"
            "  python scripts/reset_admin.py --list-users\n"
        ),
    )
    parser.add_argument("--password", default=None, help="指定新的超级管理员密码（默认随机生成）")
    parser.add_argument("--list-users", action="store_true", help="列出数据库中的注册用户")
    parser.add_argument("--promote", default=None, metavar="EMAIL", help="把指定用户提升为管理员")
    parser.add_argument("--revoke", default=None, metavar="EMAIL", help="吊销指定用户的全部登录态")
    return parser


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    Args:
        argv: 命令行参数。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(argv)
    _make_console_safe()

    if args.list_users:
        return _list_users()
    if args.promote:
        return _promote(args.promote)
    if args.revoke:
        return _revoke(args.revoke)
    return _reset_superadmin(args.password)


if __name__ == "__main__":
    raise SystemExit(main())
