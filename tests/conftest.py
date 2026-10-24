"""pytest 全局夹具。

设计要点：
    1. 在导入 ``app`` 之前把项目根塞进 ``sys.path``，让测试无需安装即可运行；
    2. 每个测试用独立的临时 storage 目录，避免污染真实的 storage/（NFR-03）；
    3. 用 ``TestClient`` 驱动完整的 ASGI 栈（含中间件与异常处理器），
       这样测到的是真实契约，而不是被 mock 掉的假象。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------- 路径准备

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 环境隔离


@pytest.fixture(scope="session", autouse=True)
def _isolate_storage() -> None:
    """把 storage 根指向临时目录，避免测试写脏真实数据。

    依赖 ``app.core.config`` 的 ``TOOLKIT_STORAGE_DIR`` 覆盖能力。
    由于该常量在模块导入时求值，本夹具必须在**任何** app 模块被导入之前生效；
    它是 session 级 autouse，且 conftest 先于测试模块被导入，因此顺序有保证。
    """
    tmp = tempfile.mkdtemp(prefix="toolkit-test-")
    os.environ["TOOLKIT_STORAGE_DIR"] = tmp
    yield
    # 测试结束后清理临时目录，避免残留（NFR-03）
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def client():
    """返回绑定到真实 ASGI 应用的测试客户端。

    Yields:
        fastapi.testclient.TestClient 实例。
    """
    from fastapi.testclient import TestClient

    from app.core.config import ensure_directories, get_settings

    ensure_directories()
    get_settings.cache_clear()

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_off(monkeypatch: pytest.MonkeyPatch):
    """确保鉴权关闭（默认即关闭，此处显式化以免受外部环境变量干扰）。

    Args:
        monkeypatch: pytest 的补丁夹具。
    """
    monkeypatch.setenv("TOOLKIT_AUTH_REQUIRED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
