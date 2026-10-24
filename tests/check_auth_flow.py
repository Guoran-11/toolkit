"""账号体系端到端自检。

用 ASGI 直连跑完整流程：发码 → 注册 → 登录 → 鉴权 → 改密吊销 → 找回密码 → 管理端。

运行方式（独立脚本，不依赖 pytest / httpx）::

    python tests/check_auth_flow.py

数据写在 ``tests/.tmp/auth`` 下，不会污染真实的 storage/。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "tests" / ".tmp" / "auth"
SCRATCH.mkdir(parents=True, exist_ok=True)
os.environ["TOOLKIT_STORAGE_DIR"] = str(SCRATCH / "storage")
os.environ["TOOLKIT_AUTH_REQUIRED"] = "true"
os.environ["TOOLKIT_ALLOW_REGISTRATION"] = "true"
sys.path.insert(0, str(ROOT))

# 某些受限环境（如沙箱）下 tempfile.mkdtemp 会创建出进程自身无写权限的目录。
# 这里替换为普通 mkdir，保证自检在任何环境下都能跑通。
_mk_counter = [0]


def _portable_mkdtemp(prefix: str = "", dir: str | None = None) -> str:
    """等价于 tempfile.mkdtemp，但不依赖平台默认权限。"""
    _mk_counter[0] += 1
    target = Path(dir or ".") / f"{prefix}chk{_mk_counter[0]}"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


tempfile.mkdtemp = _portable_mkdtemp  # type: ignore[assignment]

from app.core.config import ensure_directories, get_settings  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import create_app  # noqa: E402

ensure_directories()
init_db()

# 让超级管理员有一个已知口令（只改内存，不写 config.toml）
SUPER_PW = "SuperAdmin-9182"
settings = get_settings()
settings.server.password_hash = hash_password(SUPER_PW)

app = create_app()

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录检查结果。"""
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


async def request(
    method: str, path: str, body: bytes = b"", *, token: str | None = None, ctype: str = "application/json"
) -> tuple[int, bytes]:
    """发一个 ASGI 请求。"""
    status = 0
    chunks: list[bytes] = []
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    headers = [
        (b"host", b"127.0.0.1:5000"),
        (b"content-type", ctype.encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 5000),
    }
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, b"".join(chunks)


def jbody(data: dict) -> bytes:
    """序列化 JSON 请求体。"""
    return json.dumps(data).encode()


async def main() -> None:
    """执行全部检查。"""
    import app.core.verification as verification

    # 捕获「已发出的验证码」：日志发送器不外发，测试需要拿到它才能走完流程。
    # 顺带验证了发码确实会调用发送器。
    captured: dict[tuple[str, str], str] = {}

    def _capture(*, to: str, code: str, purpose: str, ttl_sec: int) -> None:
        captured[(to, purpose)] = code

    verification.send_mail_code = _capture  # type: ignore[assignment]

    async def send_code(email: str, purpose: str = "register") -> str:
        """调用发码接口并返回捕获到的验证码。"""
        status, body = await request(
            "POST", "/api/auth/send-code", jbody({"target": email, "channel": "email", "purpose": purpose})
        )
        assert status == 200, f"send-code 失败 status={status} body={body[:160]!r}"
        code = captured.get((email.lower(), purpose))
        assert code, f"未捕获到验证码：{captured}"
        return code

    print("=== 1. 公开配置 ===")
    status, body = await request("GET", "/api/auth/config")
    cfg = json.loads(body)
    check("GET /api/auth/config 无需登录", status == 200)
    check("暴露 allow_registration", cfg.get("allow_registration") is True)
    check("暴露 email_configured（未配 SMTP 时应为 false）", cfg.get("email_configured") is False)

    from app.core.mailer import get_mailer

    check("未配置 SMTP 时降级为日志发送器", get_mailer().name == "log")

    print("\n=== 2. 发码 → 注册 ===")
    email = "alice@example.com"
    code = await send_code(email)
    check("POST /api/auth/send-code 成功且验证码为 6 位数字", len(code) == 6 and code.isdigit(), code)

    status, _ = await request(
        "POST", "/api/auth/send-code", jbody({"target": email, "channel": "email", "purpose": "register"})
    )
    check("重发被限流（429）", status == 429, f"status={status}")

    status, body = await request(
        "POST",
        "/api/auth/register",
        jbody({"email": email, "password": "AlicePass-2026", "code": code, "display_name": "Alice"}),
    )
    reg = json.loads(body) if body.startswith(b"{") else {}
    check("注册成功并直接返回 Token", status == 200 and bool(reg.get("token")), f"status={status} {body[:140]!r}")
    alice_token = reg.get("token")
    check("注册响应含 user.role=user", (reg.get("user") or {}).get("role") == "user")
    check("注册响应不含密码哈希", "password_hash" not in body.decode(errors="replace"))

    print("\n=== 3. 注册边界 ===")
    status, _ = await request("POST", "/api/auth/register", jbody({"email": email, "password": "Another-2026", "code": "000000"}))
    check("重复邮箱被拒绝（409）", status == 409, f"status={status}")

    status, body = await request("POST", "/api/auth/send-code", jbody({"target": "bad-email", "channel": "email", "purpose": "register"}))
    check("非法邮箱被拒绝（400）", status == 400, f"status={status}")

    bob_code = await send_code("bob@example.com")
    status, body = await request("POST", "/api/auth/register", jbody({"email": "bob@example.com", "password": "short", "code": bob_code}))
    check("弱密码被拒绝（400）", status == 400, f"status={status} {body[:100]!r}")

    carol_code = await send_code("carol@example.com")
    status, _ = await request("POST", "/api/auth/register", jbody({"email": "carol@example.com", "password": "CarolPass-2026", "code": "999999"}))
    check("错误验证码被拒绝（400）", status == 400, f"status={status}")

    print("\n=== 4. 登录与鉴权 ===")
    status, body = await request("POST", "/api/auth/login", jbody({"username": email, "password": "AlicePass-2026"}))
    login = json.loads(body)
    check("邮箱 + 密码登录成功", status == 200 and bool(login.get("token")), f"status={status}")
    alice_token = login.get("token")

    status, _ = await request("POST", "/api/auth/login", jbody({"username": email, "password": "wrong"}))
    check("错误密码 401", status == 401, f"status={status}")

    status, body = await request("GET", "/api/auth/me", token=alice_token)
    me = json.loads(body)
    check("GET /api/auth/me 带 Token 成功", status == 200, f"status={status}")
    check("me 返回邮箱与角色", me.get("user", {}).get("email") == email and me["user"]["role"] == "user")
    check("me 标记非超级管理员", me.get("is_superadmin") is False)

    status, _ = await request("GET", "/api/auth/me")
    check("无 Token 访问 me 返回 401", status == 401, f"status={status}")

    status, _ = await request("GET", "/api/tasks", token=alice_token)
    check("业务接口带 Token 可访问", status == 200, f"status={status}")

    print("\n=== 5. 超级管理员 ===")
    status, body = await request("POST", "/api/auth/login", jbody({"username": settings.server.username, "password": SUPER_PW}))
    superadmin = json.loads(body)
    check("超级管理员登录成功", status == 200 and bool(superadmin.get("token")), f"status={status}")
    sa_token = superadmin.get("token")

    status, body = await request("GET", "/api/auth/me", token=sa_token)
    check("超级管理员被标记 is_superadmin", json.loads(body).get("is_superadmin") is True)

    status, _ = await request("POST", "/api/auth/login", jbody({"username": settings.server.username, "password": "nope"}))
    check("超级管理员错误密码 401", status == 401, f"status={status}")

    print("\n=== 6. 管理端点权限 ===")
    status, _ = await request("GET", "/api/admin/users", token=alice_token)
    check("普通用户访问管理端点 403", status == 403, f"status={status}")

    status, body = await request("GET", "/api/admin/users", token=sa_token)
    users = json.loads(body)
    check("超级管理员可列出用户", status == 200 and users.get("total", 0) >= 1, f"status={status} total={users.get('total')}")
    check("列表不含 password_hash", "password_hash" not in body.decode(errors="replace"))

    alice_id = next((u["id"] for u in users.get("items", []) if u["email"] == email), None)
    check("列表能查到刚注册的用户", alice_id is not None)

    if alice_id:
        status, body = await request("PATCH", f"/api/admin/users/{alice_id}", jbody({"role": "admin"}), token=sa_token)
        check("提升为管理员成功", status == 200 and json.loads(body).get("role") == "admin", f"status={status}")

        status, _ = await request("GET", "/api/admin/users", token=alice_token)
        check("角色实时生效（原 Token 即可访问管理端点）", status == 200, f"status={status}")

        status, body = await request("PATCH", f"/api/admin/users/{alice_id}", jbody({"role": "user"}), token=alice_token)
        check("管理员不能取消自己的管理员身份（403）", status == 403, f"status={status} {body[:80]!r}")

        status, _ = await request("PATCH", f"/api/admin/users/{alice_id}", jbody({"status": "disabled"}), token=alice_token)
        check("管理员不能禁用自己的账号（403）", status == 403, f"status={status}")

        status, _ = await request("DELETE", f"/api/admin/users/{alice_id}", token=alice_token)
        check("管理员不能删除自己的账号（403）", status == 403, f"status={status}")

        status, body = await request("PATCH", f"/api/admin/users/{alice_id}", jbody({"status": "disabled"}), token=sa_token)
        check("超级管理员可禁用用户", status == 200 and json.loads(body).get("status") == "disabled", f"status={status}")

        status, _ = await request("GET", "/api/auth/me", token=alice_token)
        check("禁用后旧 Token 立即失效（401）", status == 401, f"status={status}")

        status, _ = await request("POST", "/api/auth/login", jbody({"username": email, "password": "AlicePass-2026"}))
        check("被禁用的账号无法登录（403）", status == 403, f"status={status}")

    print("\n=== 7. 改密吊销 Token ===")
    dave_code = await send_code("dave@example.com")
    status, body = await request("POST", "/api/auth/register", jbody({"email": "dave@example.com", "password": "DavePass-2026", "code": dave_code}))
    dave_token = json.loads(body).get("token")
    check("dave 注册并登录", status == 200 and bool(dave_token), f"status={status}")

    status, body = await request(
        "POST", "/api/auth/password", jbody({"old_password": "DavePass-2026", "new_password": "DavePass-2027"}), token=dave_token
    )
    changed = json.loads(body)
    check("改密成功并返回新 Token", status == 200 and bool(changed.get("token")), f"status={status}")
    new_token = changed.get("token")

    status, _ = await request("GET", "/api/auth/me", token=dave_token)
    check("旧 Token 改密后立即失效（401）", status == 401, f"status={status}")
    status, _ = await request("GET", "/api/auth/me", token=new_token)
    check("新 Token 可用", status == 200, f"status={status}")
    status, _ = await request("POST", "/api/auth/login", jbody({"username": "dave@example.com", "password": "DavePass-2027"}))
    check("新密码可登录", status == 200, f"status={status}")

    print("\n=== 8. 忘记密码 ===")
    reset_code = await send_code("dave@example.com", "reset")
    check("找回密码发码成功", bool(reset_code))

    status, body = await request(
        "POST", "/api/auth/send-code", jbody({"target": "nobody@example.com", "channel": "email", "purpose": "reset"})
    )
    check("未注册邮箱返回同样的成功响应（防枚举）", status == 200 and json.loads(body).get("delivered") is False, f"status={status}")

    status, body = await request(
        "POST",
        "/api/auth/reset-password",
        jbody({"email": "dave@example.com", "code": reset_code, "new_password": "DaveReset-2028"}),
    )
    check("验证码重置密码成功", status == 200, f"status={status} {body[:100]!r}")
    status, _ = await request("POST", "/api/auth/login", jbody({"username": "dave@example.com", "password": "DaveReset-2028"}))
    check("重置后的新密码可登录", status == 200, f"status={status}")

    status, _ = await request(
        "POST",
        "/api/auth/reset-password",
        jbody({"email": "dave@example.com", "code": reset_code, "new_password": "Replay-2029"}),
    )
    check("验证码不可重放（400）", status == 400, f"status={status}")

    print("\n=== 9. 验证码尝试次数 ===")
    await send_code("erin@example.com")
    for _ in range(5):
        await request("POST", "/api/auth/register", jbody({"email": "erin@example.com", "password": "ErinPass-2026", "code": "000000"}))
    status, body = await request("POST", "/api/auth/register", jbody({"email": "erin@example.com", "password": "ErinPass-2026", "code": "000000"}))
    check("尝试超限后验证码作废（400）", status == 400, f"status={status} {body[:90]!r}")


asyncio.run(main())

print("\n" + "=" * 68)
print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
for name in FAIL:
    print(f"  失败：{name}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
