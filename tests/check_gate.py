"""登录门禁自检：确认「不输入账号密码就什么都看不到」在服务端一侧成立。

运行方式（独立脚本，不依赖 pytest / httpx）::

    python tests/check_gate.py

数据写在 ``tests/.tmp/gate`` 下，不会污染真实的 storage/。

覆盖范围：
    1. 首页默认锁定（``data-app-gate="locked"`` + 登录门禁容器存在）；
    2. 未登录时 ``/healthz`` 只返回「在线 + 需要登录」，磁盘/引擎/任务细节一概不下发；
    3. 未登录时控制面板的全部数据接口 401；
    4. 伪造 Token 与未登录同等对待；
    5. 有效 Token 可正常通过，且 ``/healthz`` 恢复完整载荷。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "tests" / ".tmp" / "gate"
SCRATCH.mkdir(parents=True, exist_ok=True)
os.environ["TOOLKIT_STORAGE_DIR"] = str(SCRATCH / "storage")
# 门禁自检的前提就是开启鉴权（默认配置也为 true，这里显式写死避免环境干扰）
os.environ["TOOLKIT_AUTH_REQUIRED"] = "true"
sys.path.insert(0, str(ROOT))

from app.core.config import ensure_directories, get_settings  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.core.security import token_manager  # noqa: E402
from app.main import create_app  # noqa: E402

ensure_directories()
init_db()
app = create_app()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


async def request(method: str, path: str, token: str | None = None) -> tuple[int, dict, bytes]:
    """发一个 ASGI 请求（不经过真实网络，直接驱动应用）。

    Args:
        method: HTTP 方法。
        path: 路径（可带查询串）。
        token: 可选 Bearer Token。

    Returns:
        ``(状态码, 响应头, 响应体)``。
    """
    status = 0
    headers: dict[str, str] = {}
    chunks: list[bytes] = []
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)

    raw_headers = [
        (b"host", b"127.0.0.1:5000"),
        (b"accept", b"application/json, text/html"),
    ]
    if token:
        raw_headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 5000),
    }
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            for k, v in message.get("headers", []):
                headers[k.decode().lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


# 未登录必须被拒绝的控制面板接口（方法, 路径）
LOCKED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/tasks"),
    ("GET", "/api/convert/matrix"),
    ("GET", "/api/system/engines"),
    ("GET", "/api/system/storage"),
    ("GET", "/api/ssh/hosts"),
    ("GET", "/api/ssh/sessions"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/admin/users"),
]

# /healthz 里属于「运行细节」的字段，未登录时必须为空
PRIVATE_HEALTH_FIELDS = ("disk_free_bytes", "uptime_sec", "tasks")


async def main() -> int:
    """执行全部门禁检查，返回进程退出码。"""
    settings = get_settings()
    check("配置生效：auth_required = true", settings.server.auth_required is True)

    # ---------- 1. 首页默认锁定 ----------
    status, _, body = await request("GET", "/")
    html = body.decode("utf-8", errors="replace")
    check("GET / 返回 200", status == 200, f"status={status}")
    check("首页默认锁定 data-app-gate=\"locked\"", 'data-app-gate="locked"' in html)
    check("首页含登录门禁容器 #auth-gate", 'id="auth-gate"' in html)
    check("首页声明了门禁样式（layout.css）", "css/layout.css" in html)

    # ---------- 2. 未登录的 /healthz ----------
    status, _, body = await request("GET", "/healthz")
    health = json.loads(body) if status == 200 else {}
    check("未登录 GET /healthz 返回 200", status == 200, f"status={status}")
    check("未登录 /healthz 标记 auth_required=true", health.get("auth_required") is True)
    check("未登录 /healthz 标记 authenticated=false", health.get("authenticated") is False)
    leaked = [f for f in PRIVATE_HEALTH_FIELDS if health.get(f) is not None]
    check("未登录 /healthz 不下发运行细节", not leaked, f"泄露字段：{leaked}")
    engines_ok = all(
        isinstance(info, dict) and info.get("state") == "unknown" and info.get("version") is None
        for info in (health.get("engines") or {}).values()
    ) and len(health.get("engines") or {}) == 3
    check("未登录 /healthz 引擎状态为 unknown", engines_ok)

    # ---------- 3. 未登录访问控制面板 ----------
    for method, path in LOCKED_ENDPOINTS:
        status, _, body = await request(method, path)
        ok = status == 401
        detail = f"status={status}"
        if not ok:
            detail += f" body={body[:120]!r}"
        check(f"未登录 {method} {path} → 401", ok, detail)

    # ---------- 4. 伪造 Token ----------
    status, _, body = await request("GET", "/api/tasks", token="forged.token.value")
    check("伪造 Token 访问 /api/tasks → 401", status == 401, f"status={status}")
    status, _, body = await request("GET", "/healthz", token="forged.token.value")
    forged_health = json.loads(body) if status == 200 else {}
    check("伪造 Token 的 /healthz 仍是最小载荷", forged_health.get("authenticated") is False)

    # ---------- 5. 有效 Token ----------
    token = token_manager.issue("admin", role="admin")["token"]

    status, _, body = await request("GET", "/api/auth/me", token=token)
    check("有效 Token GET /api/auth/me → 200", status == 200, f"status={status}")

    status, _, body = await request("GET", "/api/tasks", token=token)
    check("有效 Token GET /api/tasks → 200", status == 200, f"status={status}")

    status, _, body = await request("GET", "/healthz", token=token)
    full = json.loads(body) if status == 200 else {}
    check("有效 Token /healthz authenticated=true", full.get("authenticated") is True)
    check(
        "有效 Token /healthz 恢复完整载荷",
        isinstance(full.get("disk_free_bytes"), int) and isinstance(full.get("tasks"), dict),
    )

    # ---------- 汇总 ----------
    print()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  · {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
