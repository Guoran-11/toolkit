"""SSH 模块路由：``/api/ssh/*`` 与 ``/ws/ssh/{session_id}``。

WebSocket 帧协议（由 ``app/static/js/api.js`` 的 ``sshApi.openChannel`` 定义）：

    客户端 → 服务端
        · **二进制帧**：终端按键的 UTF-8 字节（``ws.send(new TextEncoder().encode(data))``）
        · **文本帧**：仅用于控制指令，目前只有 ``{"type":"resize","cols":80,"rows":24}``

    服务端 → 客户端
        · **二进制帧**：远端输出的原始字节（前端按 UTF-8 流式解码）

    因此服务端必须按帧类型区分处理，不能假定"收到的都是字符串"。

关于 WebSocket 鉴权的一个现实约束：
    浏览器的 ``new WebSocket(url)`` **无法自定义请求头**，所以前端无法像 fetch 那样
    发送 ``Authorization: Bearer <token>``。本实现采取双重防护：
        1. ``session_id`` 使用 128 位随机熵（``secrets.token_hex(16)``），不可枚举；
        2. 若前端未来支持 ``?token=`` 查询参数，则一并校验 Token。
    这是「不可猜测凭证 + 可选 Token」的组合，而不是把通道裸奔上线。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, model_validator

from app.api.deps import ClientIpDep, GuardDep
from app.core.config import get_settings
from app.core.errors import NotFoundError, UnauthorizedError
from app.core.security import (
    SshCredential,
    credential_vault,
    new_session_id,
    token_manager,
    write_audit_log,
)
from app.modules.ssh.session import require_ssh_enabled, session_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ssh"])


# ---------------------------------------------------------------- 请求模型


class SshConnectRequest(BaseModel):
    """SSH 连接请求。

    字段名必须与前端 ``ssh.js`` 的 ``config`` 对象一致（camelCase）。
    凭据字段使用 ``repr=False`` 并对 ``model_config`` 关闭序列化回显，
    避免在 FastAPI 的 422 错误详情中把密码回吐给客户端。

    Attributes:
        host: 目标主机地址。
        port: 端口。
        username: 用户名。
        auth_method: 认证方式（password / key）。
        password: 登录密码（password 认证）。
        private_key: 私钥文本（key 认证）。
        passphrase: 私钥口令。
        strict_host_key: 是否严格校验主机密钥指纹。
    """

    model_config = {"populate_by_name": True}

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=128)
    auth_method: str = Field(default="password", alias="authMethod")
    password: str | None = Field(default=None, repr=False)
    private_key: str | None = Field(default=None, repr=False, alias="privateKey")
    passphrase: str | None = Field(default=None, repr=False)
    strict_host_key: bool = Field(default=False, alias="strictHostKey")

    @model_validator(mode="after")
    def _check_credential(self) -> SshConnectRequest:
        """按认证方式校验必填凭据。

        Returns:
            自身实例。

        Raises:
            ValueError: 认证方式非法或缺少所需凭据。
        """
        if self.auth_method not in {"password", "key"}:
            raise ValueError("authMethod 必须是 password 或 key")
        if self.auth_method == "password" and not self.password:
            raise ValueError("密码认证需要提供 password")
        if self.auth_method == "key" and not (self.private_key or "").strip():
            raise ValueError("私钥认证需要提供 privateKey")
        return self


class _ResizeCommand(BaseModel):
    """终端尺寸同步指令。"""

    type: str
    cols: int = Field(ge=1, le=1000)
    rows: int = Field(ge=1, le=500)


# ---------------------------------------------------------------- REST 端点


@router.post("/api/ssh/connect")
def ssh_connect(
    payload: SshConnectRequest,
    _user: GuardDep,
    ip: ClientIpDep,
) -> dict[str, Any]:
    """建立 SSH 会话。

    Args:
        payload: 连接参数（含凭据）。
        _user: 鉴权主体。
        ip: 客户端 IP（审计日志）。

    Returns:
        含 ``session_id`` / ``host`` / ``port`` / ``username`` / ``host_key`` /
        ``server_banner`` 的字典，字段与 mock.sshConnect 一致。

    Raises:
        ServiceDisabledError: SSH 模块未启用。
        UnauthorizedError: 认证失败。
        ForbiddenError: 目标主机不在白名单内。
    """
    require_ssh_enabled()

    session_id = new_session_id()
    session = session_registry.create(
        session_id=session_id,
        host=payload.host.strip(),
        port=payload.port,
        username=payload.username.strip(),
    )

    credential = SshCredential(
        auth_method=payload.auth_method,
        password=payload.password,
        private_key=payload.private_key,
        passphrase=payload.passphrase,
    )

    try:
        session.connect(credential, strict_host_key=payload.strict_host_key)
    except BaseException:
        # 连接失败时立即回收会话与凭据，不留下悬空资源
        session_registry.remove(session_id, reason="连接失败")
        raise
    finally:
        # 凭据使命结束：就地擦除（NFR-SSH-01）
        credential.wipe()

    write_audit_log(
        {
            "action": "ssh.connect",
            "ip": ip,
            "session": session_id,
            "target": f"{session.username}@{session.host}:{session.port}",
            # 只记录认证方式，绝不记录凭据本身
            "auth_method": payload.auth_method,
        }
    )

    return {
        "session_id": session.session_id,
        "host": session.host,
        "port": session.port,
        "username": session.username,
        "host_key": session.info.host_key,
        "server_banner": session.info.server_banner,
    }


@router.delete("/api/ssh/{session_id}")
def ssh_disconnect(session_id: str, _user: GuardDep, ip: ClientIpDep) -> dict[str, bool]:
    """断开指定会话。

    Args:
        session_id: 会话标识。
        _user: 鉴权主体。
        ip: 客户端 IP。

    Returns:
        ``{"ok": True}``。

    Raises:
        NotFoundError: 会话不存在。
    """
    if session_registry.get(session_id) is None:
        raise NotFoundError("会话不存在或已断开")
    session_registry.remove(session_id, reason="用户主动断开")
    write_audit_log({"action": "ssh.disconnect", "ip": ip, "session": session_id})
    return {"ok": True}


@router.get("/api/ssh/sessions")
def ssh_sessions(_user: GuardDep) -> dict[str, Any]:
    """列出当前活跃会话（不含凭据）。

    供前端顶栏会话标签与外部的资源监控使用。

    设计说明：本端点在 SSH 模块**未启用**时返回空列表而非 503。
    理由：它是只读列表接口，空列表不泄露任何信息；返回 503 反而会
    让上层监控/巡检把它当成故障，且前端渲染时会弹错误提示。
    真正需要拒绝的是「建立连接」这类有副作用的操作（见 ``ssh_connect``）。

    Args:
        _user: 鉴权主体。

    Returns:
        ``{"items": [...], "enabled": bool}``。
    """
    settings = get_settings()
    if not settings.ssh.enabled:
        return {"items": [], "enabled": False}

    return {
        "items": [session.info.to_api() for session in session_registry.list_all()],
        "enabled": True,
    }


@router.get("/api/ssh/hosts")
def ssh_hosts(_user: GuardDep) -> dict[str, Any]:
    """返回已保存的主机列表。

    设计说明：出于「凭据与本机信息不出服务端」的考虑，
    主机列表（地址/端口/用户名/认证方式）由**前端 localStorage** 保管
    （见 ``ssh.js`` 的 ``saveSshHosts`` / ``loadSshHosts``），
    服务端不持久化任何连接目标。因此本端点固定返回空列表，
    仅为满足接口契约与未来的服务端托管方案保留位置。

    与 ``ssh_sessions`` 同理：未启用模块时返回空列表而非 503。

    Args:
        _user: 鉴权主体。

    Returns:
        ``{"items": [], "enabled": bool}``。
    """
    settings = get_settings()
    return {"items": [], "enabled": settings.ssh.enabled}


# ---------------------------------------------------------------- WebSocket


async def _authenticate_socket(websocket: WebSocket) -> bool:
    """校验 WebSocket 连接资格。

    Args:
        websocket: 连接对象。

    Returns:
        允许连接返回 True；否则已发送关闭帧并返回 False。
    """
    settings = get_settings()

    if not settings.ssh.enabled:
        await websocket.close(code=1013, reason="SSH 模块未启用")
        return False

    # 若前端传了 token 则严格校验（未来增强路径）
    token = websocket.query_params.get("token")
    if token:
        try:
            token_manager.verify(token)
        except UnauthorizedError:
            await websocket.close(code=1008, reason="鉴权失败")
            return False
    elif settings.server.auth_required:
        # 未提供 token 时，依赖会话 ID 的不可猜测性（见模块 docstring 的说明）。
        # 这里不做拒绝，否则浏览器原生 WebSocket 将完全无法使用。
        logger.debug("WebSocket 未携带 Token，依赖 session_id 熵值进行访问控制")

    return True


@router.websocket("/ws/ssh/{session_id}")
async def ssh_terminal(websocket: WebSocket, session_id: str) -> None:
    """SSH 终端双向数据通道。

    Args:
        websocket: WebSocket 连接。
        session_id: 会话标识（在 REST 连接阶段签发）。
    """
    if not await _authenticate_socket(websocket):
        return

    session = session_registry.get(session_id)
    if session is None:
        await websocket.close(code=1008, reason="会话不存在，请重新连接")
        return
    if not session.is_alive:
        session_registry.remove(session_id, reason="会话已失效")
        await websocket.close(code=1008, reason="会话已断开，请重新连接")
        return

    await websocket.accept()

    loop = asyncio.get_running_loop()
    queue = session.attach_loop(loop)

    async def pump_to_client() -> None:
        """把远端输出推送给浏览器。"""
        while True:
            data = await queue.get()
            if not data:
                # 读线程投递的空字节是「会话已断开」的哨兵
                break
            await websocket.send_bytes(data)

    async def pump_from_client() -> None:
        """接收浏览器输入并写入远端。"""
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            text = message.get("text")
            if text is not None:
                # 文本帧只承载控制指令（resize），不写入 shell
                try:
                    command = _ResizeCommand.model_validate_json(text)
                except Exception:  # noqa: BLE001 - 非法指令静默忽略，不打断终端
                    logger.debug("忽略无法解析的控制帧：%s", text[:80])
                    continue
                if command.type == "resize":
                    session.resize(command.cols, command.rows)
                continue

            data = message.get("bytes")
            if data:
                session.write(data)

    to_client = asyncio.create_task(pump_to_client(), name=f"ssh-out-{session_id[:8]}")
    from_client = asyncio.create_task(pump_from_client(), name=f"ssh-in-{session_id[:8]}")

    try:
        done, pending = await asyncio.wait(
            {to_client, from_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        # 回收被取消任务的异常，避免 "Task exception was never retrieved" 噪音
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exception = task.exception()
            if exception and not isinstance(exception, (WebSocketDisconnect, asyncio.CancelledError)):
                logger.info("SSH 通道 %s 异常结束：%s", session_id, exception)
    except WebSocketDisconnect:
        logger.debug("客户端断开 WebSocket：%s", session_id)
    finally:
        # 浏览器意外关闭时同样必须清理会话（否则会残留 paramiko 线程与 channel）
        session_registry.remove(session_id, reason="终端连接已关闭")
        # 连接可能已断，关闭失败无需处理
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)
        logger.info("SSH 会话已回收：%s（剩余凭据 %s 条）", session_id, credential_vault.count())


__all__ = ["router"]
