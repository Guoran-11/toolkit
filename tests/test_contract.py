"""API 契约测试。

目的：把前端 ``app/static/js/api.js`` 的调用与后端实际响应放在一起对撞，
确保字段名、结构、状态码逐一对齐。任何一个字段写错都会在这里暴露。

覆盖策略：
    · 存在性 —— 前端会调用的每个端点在 OpenAPI 里都存在（拼写错误即失败）；
    · 结构   —— 响应体的关键字段名与前端读取的键严格一致；
    · 语义   —— 错误路径返回统一错误体（``detail`` + ``code``）。
"""

from __future__ import annotations

import io

import pytest

# 前端 api.js 会调用的全部端点（方法, 路径模板）。
# 这张表是"契约的契约"——改动前端调用后必须同步更新，否则测试失去意义。
FRONTEND_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/healthz"),
    ("GET", "/api/system/engines"),
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/send-code"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/reset-password"),
    ("GET", "/api/admin/users"),
    ("PATCH", "/api/admin/users/{user_id}"),
    ("DELETE", "/api/admin/users/{user_id}"),
    ("GET", "/api/convert/matrix"),
    ("POST", "/api/convert"),
    ("POST", "/api/convert/batch-zip"),
    ("POST", "/api/convert/pdf-tools"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("DELETE", "/api/tasks/{task_id}"),
    ("GET", "/api/download/{file_id}"),
    ("POST", "/api/audio/probe"),
    ("POST", "/api/audio/convert"),
    ("POST", "/api/md/export"),
    ("POST", "/api/ssh/connect"),
    ("GET", "/api/ssh/hosts"),
    ("GET", "/api/ssh/sessions"),
    ("DELETE", "/api/ssh/{session_id}"),
    # 视频下载（契约见 docs/视频下载模块-实现契约.md §3）
    ("GET", "/api/video/platforms"),
    ("GET", "/api/video/settings"),
    ("PUT", "/api/video/settings"),
    ("GET", "/api/video/history"),
    ("POST", "/api/video/parse"),
    ("POST", "/api/video/download"),
]


def _openapi_paths() -> set[str]:
    """取应用的 OpenAPI 路径集合。

    Returns:
        形如 ``{"GET /api/tasks", "DELETE /api/tasks/{task_id}", ...}`` 的集合。
    """
    from app.main import create_app

    schema = create_app().openapi()
    result: set[str] = set()
    for path, methods in schema.get("paths", {}).items():
        for method in methods:
            result.add(f"{method.upper()} {path}")
    return result


# ---------------------------------------------------------------- 存在性


def test_all_frontend_endpoints_registered() -> None:
    """前端调用的每个端点都必须在后端注册（含 WebSocket）。

    这条测试是防"手滑拼错路径"的第一道闸门。
    """
    registered = _openapi_paths()
    missing = [
        f"{method} {path}" for method, path in FRONTEND_ENDPOINTS if f"{method} {path}" not in registered
    ]
    assert not missing, f"以下前端端点在后端缺失：{missing}"


def _collect_paths(routes) -> list[str]:
    """递归收集全部路由路径，兼容新版 FastAPI 的 ``_IncludedRouter`` 包装。

    说明：FastAPI >= 0.13x 的 ``include_router`` 不再把子路由扁平化进
    ``app.routes``，而是包成 ``_IncludedRouter``（真实路由在其
    ``original_router.routes`` 上）。因此遍历时必须两条路径都走。

    Args:
        routes: 路由对象集合。

    Returns:
        路径字符串列表。
    """
    found: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            found.append(path)

        sub = getattr(route, "routes", None)
        if sub:
            found.extend(_collect_paths(sub))

        inner = getattr(route, "original_router", None)
        inner_routes = getattr(inner, "routes", None)
        if inner_routes:
            found.extend(_collect_paths(inner_routes))
    return found


def test_websocket_ssh_route_registered() -> None:
    """SSH WebSocket 通道必须存在（OpenAPI 不含 WS，需单独检查路由表）。"""
    from app.main import create_app

    paths = _collect_paths(create_app().routes)
    assert any(p.startswith("/ws/ssh") for p in paths), f"未找到 WS 路由，现有：{sorted(set(paths))}"


def test_websocket_ssh_guards_disabled_module(client) -> None:
    """SSH 未启用时，WebSocket 必须被拒绝而不是无条件放行（安全默认值）。"""
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/ws/ssh/does-not-exist") as ws,
    ):
        ws.receive_text()
    # 1013 = 稍后再试（模块禁用）；1008 = 策略拒绝（会话不存在）
    assert excinfo.value.code in (1008, 1013)


# ---------------------------------------------------------------- /healthz


def test_healthz_contract(client, auth_off) -> None:
    """``/healthz`` 的字段必须满足前端 ``probeBackend`` 的读取逻辑。

    前端读取：``version`` / ``disk_free_bytes`` / ``engines`` / ``auth_required``。
    """
    res = client.get("/healthz")
    assert res.status_code == 200, res.text
    data = res.json()

    for key in ("ok", "version", "disk_free_bytes", "engines", "auth_required"):
        assert key in data, f"/healthz 缺少前端依赖的字段：{key}"

    assert isinstance(data["auth_required"], bool)
    # engines 必须是三键字典，前端 normalizeEngines 会逐个读取
    for engine in ("libreoffice", "chromium", "ffmpeg"):
        assert engine in data["engines"], f"/healthz.engines 缺少 {engine}"


def test_healthz_engine_status_shape(client, auth_off) -> None:
    """引擎状态项需含 state/version/path/hint，否则前端渲染会拿到 undefined。"""
    data = client.get("/healthz").json()
    for name, status in data["engines"].items():
        assert isinstance(status, dict), f"{name} 状态应为对象"
        for field in ("state", "version", "path", "hint"):
            assert field in status, f"{name} 状态缺少 {field}"


# ---------------------------------------------------------------- 登录门禁

# 主界面准入的核心断言：未登录时这些接口一律拒绝，且 /healthz 不泄露运行细节。
_LOCKED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/tasks"),
    ("GET", "/api/convert/matrix"),
    ("GET", "/api/system/engines"),
    ("GET", "/api/system/storage"),
    ("GET", "/api/ssh/hosts"),
    ("GET", "/api/ssh/sessions"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/admin/users"),
]


def _auth_on(client):
    """在**已建好的 app** 上临时打开鉴权开关。

    必须改配置单例本身而不是重建 app：路由依赖是每次请求现取 ``get_settings()``，
    而 TestClient 已在建 app 时缓存了实例。返回原值供调用方恢复。

    Returns:
        ``(settings, 原值)``。
    """
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.server.auth_required
    settings.server.auth_required = True
    return settings, original


def test_healthz_hides_runtime_details_when_locked(client) -> None:
    """鉴权开启且未登录时，/healthz 只能返回「在线 + 需要登录」这一最小事实。

    磁盘余量、引擎清单、任务计数、运行时长都指向同一套部署信息，未登录访客
    没有理由看到。字段名保持不变（值为 None/unknown），前端探测逻辑无需分支。
    """
    settings, original = _auth_on(client)
    try:
        data = client.get("/healthz").json()
        assert data["ok"] is True
        assert data["auth_required"] is True
        assert data["authenticated"] is False
        # 不泄露运行细节
        assert data["disk_free_bytes"] is None
        assert data["uptime_sec"] is None
        assert data["tasks"] is None
        for name, status in data["engines"].items():
            assert status["state"] == "unknown", f"{name} 未登录时不应暴露真实状态"
            assert status["version"] is None and status["path"] is None
    finally:
        settings.server.auth_required = original


def test_healthz_full_payload_with_valid_token(client) -> None:
    """携带有效 Token 时 /healthz 恢复完整载荷（登录后前端补拉状态靠它）。"""
    from app.core.security import token_manager

    settings, original = _auth_on(client)
    try:
        token = token_manager.issue("admin", role="admin")["token"]
        res = client.get("/healthz", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["authenticated"] is True
        assert isinstance(data["disk_free_bytes"], int)
        assert isinstance(data["tasks"], dict)
        # 完整载荷里引擎提示不再是「登录后可查看」，说明走的确实是授权分支
        for status in data["engines"].values():
            assert status["hint"] != "登录后可查看引擎状态。"
    finally:
        settings.server.auth_required = original


@pytest.mark.parametrize(("method", "path"), _LOCKED_ENDPOINTS)
def test_locked_panel_endpoints_reject_anonymous(client, method: str, path: str) -> None:
    """未登录时，控制面板的每一个数据接口都必须 401（「什么也不能看」的服务端一侧）。"""
    settings, original = _auth_on(client)
    try:
        res = client.request(method, path)
        assert res.status_code == 401, f"{method} {path} 未登录却返回 {res.status_code}"
        body = res.json()
        assert "detail" in body and "code" in body
    finally:
        settings.server.auth_required = original


def test_locked_panel_endpoints_reject_invalid_token(client) -> None:
    """伪造/失效 Token 与未登录同等对待，不能因为「带了头」就放行。"""
    settings, original = _auth_on(client)
    try:
        res = client.get("/api/tasks", headers={"Authorization": "Bearer not.a.real.token"})
        assert res.status_code == 401, res.text
    finally:
        settings.server.auth_required = original


def test_index_html_locks_panel_before_login(client, auth_off) -> None:
    """首页必须默认处于「锁定」状态。

    面板外壳是静态标记，若不在 HTML 阶段就锁住，首帧会闪出控制面板骨架——
    这正是「先登录才能进入控制面板」要避免的观感。
    """
    res = client.get("/")
    assert res.status_code == 200, res.text
    html = res.text
    assert 'data-app-gate="locked"' in html, "index.html 未声明默认锁定状态"
    assert 'id="auth-gate"' in html, "index.html 缺少登录门禁容器"


# ---------------------------------------------------------------- 系统


def test_system_engines_contract(client, auth_off) -> None:
    """``/api/system/engines`` 返回 ``{engines: {...}}``。"""
    res = client.get("/api/system/engines")
    assert res.status_code == 200, res.text
    data = res.json()
    assert "engines" in data and isinstance(data["engines"], dict)
    assert "summary" in data


# ---------------------------------------------------------------- 账号体系


def test_auth_config_is_public(client, auth_off) -> None:
    """``/api/auth/config`` 必须无需登录即可访问（前端据此渲染注册入口）。"""
    res = client.get("/api/auth/config")
    assert res.status_code == 200, res.text
    data = res.json()
    for key in (
        "allow_registration",
        "require_email_verification",
        "login_require_code",
        "min_password_length",
        "code_ttl_sec",
        "code_resend_sec",
        "email_configured",
        "sms_configured",
    ):
        assert key in data, f"/api/auth/config 缺少前端依赖的字段：{key}"
    assert isinstance(data["allow_registration"], bool)
    assert isinstance(data["min_password_length"], int)


def test_send_code_rejects_invalid_email(client, auth_off) -> None:
    """非法邮箱在发码阶段就被拒绝，且错误体符合统一契约。"""
    # 用 reset 用途：register 用途会先被「未开放注册」拦下，测不到邮箱校验
    res = client.post(
        "/api/auth/send-code",
        json={"target": "not-an-email", "channel": "email", "purpose": "reset"},
    )
    assert res.status_code == 400, res.text
    body = res.json()
    assert "detail" in body and "code" in body


def test_register_rejected_when_disabled(client, auth_off) -> None:
    """默认不开放注册时，注册端点必须明确拒绝（而不是静默建号）。"""
    res = client.post(
        "/api/auth/register",
        json={"email": "nobody@example.com", "password": "SomePass-1234", "code": "000000"},
    )
    assert res.status_code == 403, f"注册未被拒绝：{res.status_code} {res.text}"


def test_admin_endpoints_require_auth(client) -> None:
    """管理端点必须要求登录：开启鉴权后未带 Token 一律 401。"""
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.server.auth_required
    settings.server.auth_required = True
    try:
        res = client.get("/api/admin/users")
        assert res.status_code == 401, res.text
    finally:
        settings.server.auth_required = original


# ---------------------------------------------------------------- 转换矩阵


def test_matrix_contract(client, auth_off) -> None:
    """转换矩阵是前端渲染格式下拉框的数据源，结构必须稳定。"""
    res = client.get("/api/convert/matrix")
    assert res.status_code == 200, res.text
    data = res.json()
    # 前端 matrix.js 消费 routes / formats 之类的字段；这里断言非空且为集合型
    assert isinstance(data, dict)
    assert data, "矩阵不应为空"


# ---------------------------------------------------------------- 任务中心


def test_tasks_list_contract(client, auth_off) -> None:
    """任务列表响应字段名（snake_case）与前端读取一致。"""
    res = client.get("/api/tasks", params={"pageSize": 200})
    assert res.status_code == 200, res.text
    data = res.json()
    for key in ("total", "page", "page_size", "items"):
        assert key in data, f"任务列表缺少字段 {key}"
    assert isinstance(data["items"], list)


def test_task_not_found_returns_error_body(client, auth_off) -> None:
    """不存在的任务返回 404，且错误体含 detail + code。"""
    res = client.get("/api/tasks/does-not-exist")
    assert res.status_code == 404, res.text
    body = res.json()
    assert "detail" in body
    assert "code" in body


# ---------------------------------------------------------------- 下载安全


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "..%2f..%2fwin.ini",
        "a/b",
        "a\\b",
        "....//",
        "x" * 200,
    ],
)
def test_download_rejects_unsafe_file_id(client, auth_off, bad_id: str) -> None:
    """file_id 必须拒绝路径穿越与非法字符（NFR-04 安全约束）。"""
    res = client.get(f"/api/download/{bad_id}")
    assert res.status_code in (400, 404), f"危险 file_id 未被拒绝：{bad_id} -> {res.status_code}"


def test_download_unknown_id_returns_404(client, auth_off) -> None:
    """合法格式但不存在于登记的 id 返回 404，而非 500。"""
    res = client.get("/api/download/deadbeefdeadbeef")
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------- 鉴权


def test_login_rejects_wrong_password(client) -> None:
    """错误口令返回 401（即便鉴权关闭，登录端点自身仍需校验）。"""
    res = client.post("/api/auth/login", json={"username": "admin", "password": "definitely-wrong"})
    assert res.status_code in (401, 403), res.text
    body = res.json()
    assert "detail" in body


def test_login_rejects_empty_credentials(client) -> None:
    """空凭据返回 400，不泄露任何账号信息。"""
    res = client.post("/api/auth/login", json={"username": "", "password": ""})
    assert res.status_code == 400, res.text


# ---------------------------------------------------------------- 音频


def test_audio_probe_is_per_file_tolerant(client, auth_off) -> None:
    """探测必须**逐文件**返回结果：一个坏文件不能让整批失败。

    这是前端 ``audio.js`` 的核心假设——它遍历 ``data.results`` 并对
    每条渲染 ``decryptable`` 标记。若整批 400，同批的有效文件会被一并
    标成「无法识别」，属于真实缺陷。
    """
    files = [
        ("files", ("garbage.txt", io.BytesIO(b"hello world, definitely not audio"), "text/plain")),
        ("files", ("song.mp3", io.BytesIO(b"\xff\xfb\x90\x00" + b"\x00" * 64), "audio/mpeg")),
    ]
    res = client.post("/api/audio/probe", files=files)
    assert res.status_code == 200, res.text

    results = res.json().get("results")
    assert isinstance(results, list)
    # 关键：两个文件各有一条结果，坏文件被标记为不可用而非整批失败
    assert len(results) == 2, f"应逐文件返回，实际 {results}"

    by_name = {item["name"]: item for item in results}
    assert by_name["garbage.txt"]["decryptable"] is False
    for item in results:
        for field in ("name", "detected_type", "detected_label", "decryptable"):
            assert field in item, f"探测结果缺少字段 {field}"


# ---------------------------------------------------------------- SSH


def test_ssh_hosts_and_sessions(client, auth_off) -> None:
    """SSH 列表端点即使模块关闭也应返回 200 + 空结构，而非 503。

    设计取舍：只读列表接口返回空数据不泄露信息，且不会让前端/巡检误判为故障；
    真正需要拒绝的是有副作用的 ``connect``。
    """
    hosts = client.get("/api/ssh/hosts")
    assert hosts.status_code == 200, hosts.text
    hosts_body = hosts.json()
    assert "items" in hosts_body and isinstance(hosts_body["items"], list)
    assert hosts_body.get("enabled") is False  # 默认关闭

    sessions = client.get("/api/ssh/sessions")
    assert sessions.status_code == 200, sessions.text
    sessions_body = sessions.json()
    assert isinstance(sessions_body.get("items"), list)


# ---------------------------------------------------------------- 视频下载


def test_video_endpoints_contract(client, auth_off) -> None:
    """视频下载模块的端点与只读响应字段必须符合契约 §3。

    断言三件事：
        1. ``GET/PUT /api/video/*`` 六个端点在 OpenAPI 中注册（拼错即失败）；
        2. ``/platforms`` 返回三平台条目，且条目里前端读取的键齐全；
        3. ``/settings`` 与 ``/history`` 的字段名与契约一致。

    说明：这里只跑**只读**端点。``PUT /api/video/settings`` 会把设置写回
    ``config.toml``，契约测试不应留下副作用，写回路径由 ``tests/test_video.py``
    在受控存储目录内验证。
    """
    from app.core.config import get_settings

    settings = get_settings()
    video_cfg = getattr(settings, "video", None)
    assert video_cfg is not None, "Settings 缺少 video 段（见契约 §2.2）"
    original_enabled = video_cfg.enabled
    video_cfg.enabled = True  # 契约默认开启；测试显式化避免受 config.toml 影响
    try:
        registered = _openapi_paths()
        missing = [
            f"{method} {path}"
            for method, path in (
                ("GET", "/api/video/platforms"),
                ("GET", "/api/video/settings"),
                ("PUT", "/api/video/settings"),
                ("GET", "/api/video/history"),
                ("POST", "/api/video/parse"),
                ("POST", "/api/video/download"),
            )
            if f"{method} {path}" not in registered
        ]
        assert not missing, f"视频下载端点在后端缺失：{missing}"

        # 历史删除（契约 §3.4）在路由表里，但路径参数名未写死，只断言前缀
        paths = _collect_paths(client.app.routes)  # type: ignore[attr-defined]
        assert any(p.startswith("/api/video/history") for p in paths), paths

        res = client.get("/api/video/platforms")
        assert res.status_code == 200, res.text
        data = res.json()
        for key in ("enabled", "items", "max_urls", "prefer_quality", "prefer_format"):
            assert key in data, f"/api/video/platforms 缺少字段 {key}"
        assert isinstance(data["items"], list)
        assert {item["name"] for item in data["items"]} == {"douyin", "bilibili", "kuaishou"}
        for item in data["items"]:
            for key in ("name", "label", "enabled", "hosts", "capabilities", "cookie"):
                assert key in item, f"平台条目 {item.get('name')} 缺少字段 {key}"
            assert isinstance(item["hosts"], list) and item["hosts"]
            assert isinstance(item["capabilities"], dict)
            assert "configured" in item["cookie"]

        settings_res = client.get("/api/video/settings")
        assert settings_res.status_code == 200, settings_res.text
        settings_data = settings_res.json()
        for key in (
            "enabled",
            "prefer_quality",
            "prefer_format",
            "max_concurrent",
            "download_segments",
            "max_file_mb",
            "platforms",
        ):
            assert key in settings_data, f"/api/video/settings 缺少字段 {key}"
        assert set(settings_data["platforms"]) == {"douyin", "bilibili", "kuaishou"}

        history_res = client.get("/api/video/history")
        assert history_res.status_code == 200, history_res.text
        history_data = history_res.json()
        for key in ("total", "page", "page_size", "items"):
            assert key in history_data, f"/api/video/history 缺少字段 {key}"
        assert isinstance(history_data["items"], list)
    finally:
        video_cfg.enabled = original_enabled


# ---------------------------------------------------------------- 错误体统一性


def test_unknown_api_path_returns_json_error(client, auth_off) -> None:
    """未匹配的 /api 路径返回统一 JSON 错误体，便于前端 parseErrorBody 解析。"""
    res = client.get("/api/this/does/not/exist")
    assert res.status_code == 404
    body = res.json()
    assert "detail" in body
    assert "code" in body
