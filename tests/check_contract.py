"""契约回归自检：确认账号体系改造没有破坏原有接口。

运行方式（独立脚本，不依赖 pytest / httpx）::

    python tests/check_contract.py

数据写在 ``tests/.tmp/contract`` 下，不会污染真实的 storage/。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "tests" / ".tmp" / "contract"
SCRATCH.mkdir(parents=True, exist_ok=True)
os.environ["TOOLKIT_STORAGE_DIR"] = str(SCRATCH / "storage")
os.environ["TOOLKIT_AUTH_REQUIRED"] = "false"
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

from app.core.config import ensure_directories  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.main import create_app  # noqa: E402

ensure_directories()
init_db()
app = create_app()

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录检查结果。"""
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


async def request(method: str, path: str, body: bytes = b"", ctype: str = "application/json") -> tuple[int, dict, bytes]:
    """发一个 ASGI 请求。"""
    status = 0
    headers: dict[str, str] = {}
    chunks: list[bytes] = []
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"127.0.0.1:5000"), (b"content-type", ctype.encode()),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 5000),
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
            for k, v in message.get("headers", []):
                headers[k.decode().lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


def multipart(files: list[tuple[str, str, bytes]], fields: dict[str, str]) -> tuple[bytes, str]:
    """构造 multipart 请求体。"""
    boundary = "----reg7f3a1b"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    for field, filename, data in files:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


async def main() -> None:
    """执行回归检查。"""
    status, headers, body = await request("GET", "/healthz")
    data = json.loads(body)
    check("GET /healthz 200 且契约字段齐全", status == 200 and data.get("ok") is True
          and all(k in data for k in ("ok", "version", "disk_free_bytes", "engines", "auth_required")))
    check("CSP 仍下发", "content-security-policy" in headers)

    status, _, body = await request("GET", "/api/system/engines")
    check("GET /api/system/engines 200", status == 200 and "summary" in json.loads(body))

    status, _, body = await request("GET", "/api/system/storage")
    check("GET /api/system/storage 200", status == 200 and "outputs" in json.loads(body))

    status, _, body = await request("GET", "/api/convert/matrix")
    matrix = json.loads(body)
    check("GET /api/convert/matrix 结构完整",
          status == 200 and all(k in matrix for k in ("matrix", "sources", "targets", "engines", "limits")))

    status, _, body = await request("GET", "/api/tasks?pageSize=200")
    tasks = json.loads(body)
    check("GET /api/tasks 契约字段齐全",
          status == 200 and all(k in tasks for k in ("total", "page", "page_size", "items")))

    status, _, body = await request("GET", "/api/tasks/nope")
    err = json.loads(body)
    check("统一错误体", status == 404 and "detail" in err and "code" in err)

    status, _, _ = await request("GET", "/api/download/../../etc/passwd")
    check("路径穿越被拒绝", status in (400, 404), f"status={status}")

    status, _, body = await request("GET", "/api/ssh/hosts")
    check("SSH hosts 200 + 空列表", status == 200 and json.loads(body).get("items") == [])

    status, _, _ = await request("GET", "/api/ssh/sessions")
    check("SSH sessions 200", status == 200)

    status, _, _ = await request("POST", "/api/auth/login", json.dumps({"username": "admin", "password": "wrong"}).encode())
    check("错误口令 401", status == 401, f"status={status}")

    status, _, _ = await request("POST", "/api/auth/login", json.dumps({"username": "", "password": ""}).encode())
    check("空凭据 400", status == 400, f"status={status}")

    payload, ctype = multipart(
        [("files", "garbage.txt", b"hello world, definitely not audio"),
         ("files", "song.mp3", b"\xff\xfb\x90\x00" + b"\x00" * 64)], {})
    status, _, body = await request("POST", "/api/audio/probe", payload, ctype)
    results = json.loads(body).get("results", [])
    by_name = {i["name"]: i for i in results}
    check("音频探测逐文件容错", status == 200 and len(results) == 2
          and by_name["garbage.txt"]["decryptable"] is False
          and by_name["song.mp3"]["decryptable"] is True)

    payload, ctype = multipart([("files", "fake.pdf", b"this is plainly text, not a pdf")],
                               {"source_format": "pdf", "target_format": "png"})
    status, _, body = await request("POST", "/api/convert", payload, ctype)
    check("伪装扩展名 415", status == 415 and json.loads(body)["code"] == "UNSUPPORTED_FORMAT")

    md = json.dumps({
        "html": "<html><head><title>T</title></head><body><pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre></body></html>",
        "filename": "t.md", "format": "html",
    }).encode()
    status, _, body = await request("POST", "/api/md/export", md)
    exported = json.loads(body)
    check("Markdown 导出 HTML 成功", status == 200 and exported.get("status") == "success", f"status={status}")
    out = exported.get("output") or {}
    if out.get("file_id"):
        st, _, dl = await request("GET", f"/api/download/{out['file_id']}")
        text = dl.decode("utf-8", errors="replace")
        check("导出内容 XSS 已修复且为完整文档",
              st == 200 and "<script>alert(1)</script>" not in text and "&lt;script&gt;" in text
              and "<!DOCTYPE html>" in text)

    status, _, _ = await request("GET", "/index.html")
    check("前端首页可访问", status == 200)

    status, _, body = await request("GET", "/api/auth/config")
    check("新增的公开鉴权配置端点可用", status == 200 and "allow_registration" in json.loads(body))

    # 未匹配路径仍返回统一 JSON 错误体
    status, _, body = await request("GET", "/api/this/does/not/exist")
    check("未匹配 /api 路径返回统一错误体", status == 404 and "code" in json.loads(body))


asyncio.run(main())
print("\n" + "=" * 68)
print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
for name in FAIL:
    print(f"  失败：{name}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
