"""启动脚本。

用法：
    python run.py                          # 默认 127.0.0.1:5000，自动打开浏览器
    python run.py --port 8080              # 指定起始端口（被占用时自动顺延）
    python run.py --lan                    # 允许局域网访问（强制开启鉴权）
    python run.py --no-browser             # 服务器/容器中运行，不打开浏览器
    python run.py --reload                 # 开发模式，代码热重载

为什么不用 typer：本脚本会被 PyInstaller 打包成离线单机版入口，
每多一个第三方依赖就多一份打包体积与启动解析开销。argparse 完全够用。

端口自动顺延的意义：单机版用户可能同时开着别的服务（比如已有的 5000 端口应用），
直接报 "address already in use" 会让人不知所措，自动顺延并打印实际端口才是好体验。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 允许以 `python run.py` 直接运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("toolkit.run")

# 端口顺延的尝试次数
_PORT_SCAN_RANGE = 50


def find_free_port(host: str, start: int, span: int = _PORT_SCAN_RANGE) -> int:
    """从 ``start`` 开始寻找一个可绑定的端口。

    Args:
        host: 绑定地址。
        start: 起始端口。
        span: 最多尝试多少个连续端口。

    Returns:
        可用的端口号。

    Raises:
        RuntimeError: 连续 ``span`` 个端口都被占用。
    """
    # 0.0.0.0 与 127.0.0.1 的可用性不同：绑定通配地址会与其它监听冲突更少，
    # 但检测时必须用实际要绑定的地址，否则会误判。
    probe_host = "0.0.0.0" if host in {"0.0.0.0", "::"} else "127.0.0.1"

    for offset in range(span):
        port = start + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # 必须与 uvicorn 的绑定语义一致：uvicorn 会设置 SO_REUSEADDR，
            # 因此仅处于 TIME_WAIT 的旧连接不会妨碍它绑定。若这里不设置，
            # 上一次进程退出后残留的 TIME_WAIT 会让探测误判为「端口被占用」，
            # 服务于是漂移到 50001 之类的端口（2026-10-13 实测复现）。
            # 注意：SO_REUSEADDR 不能抢占仍处于 LISTEN 的端口，
            # 所以「真的被别人监听」依旧会被正确识别为占用。
            if os.name == "posix":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((probe_host, port))
                return port
            except OSError:
                continue

    raise RuntimeError(
        f"从 {start} 开始的 {span} 个端口均被占用，请用 --port 指定其它端口。"
    )


def open_browser_later(url: str, delay: float = 1.6) -> None:
    """延迟打开浏览器。

    延迟是必要的：uvicorn 的启动信号在 socket 就绪前就会返回，
    立即打开会得到一个「无法连接」的页面，用户往往就此认为服务没起来。

    Args:
        url: 要打开的地址。
        delay: 延迟秒数。
    """

    def _open() -> None:
        """执行打开动作。"""
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - 无图形环境时打开失败属正常
            logger.info("无法自动打开浏览器：%s", exc)

    threading.Thread(target=_open, name="open-browser", daemon=True).start()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        prog="toolkit",
        description="全能工具站：文档转换 / SSH 终端 / 音频转换 / Markdown 预览",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py                    # 本机使用，自动打开浏览器\n"
            "  python run.py --lan              # 局域网共享（强制开启登录鉴权）\n"
            "  python run.py --port 8000 --no-browser\n"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="监听地址（默认取 config.toml，用 --lan 时强制为 0.0.0.0）",
    )
    parser.add_argument("--port", type=int, default=None, help="起始端口（默认 5000，被占用时自动顺延）")
    parser.add_argument("--lan", action="store_true", help="允许局域网访问（会自动开启登录鉴权）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    parser.add_argument("--log-level", default="info", help="日志级别（debug/info/warning/error）")
    parser.add_argument("--workers", type=int, default=1, help="uvicorn worker 进程数（默认 1）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    Args:
        argv: 命令行参数（默认取 sys.argv）。

    Returns:
        进程退出码（0 表示正常退出）。
    """
    args = build_parser().parse_args(argv)

    # 延迟导入：--help 不需要拉起整个应用（也避免依赖缺失时报错难懂）
    try:
        import uvicorn
    except ImportError:
        print(  # noqa: T201
            "缺少依赖 uvicorn。请先安装依赖：\n"
            "    pip install -r requirements.txt\n"
            "或使用 uv：\n"
            "    uv sync",
            file=sys.stderr,
        )
        return 2

    from app.core.config import ensure_directories, get_settings

    settings = get_settings()
    ensure_directories()

    # 命令行优先于配置文件
    if args.lan:
        # 局域网暴露必须开启鉴权（§9.1 第 6 条为硬性约束）。
        # get_settings() 是进程级单例，此处直接改属性即可全局生效。
        settings.server.allow_lan = True
        settings.server.auth_required = True
        if not settings.server.password_hash:
            from app.core.security import bootstrap_password

            generated = bootstrap_password()
            if generated:
                print(f"已生成管理员初始密码：{generated}（用户名 {settings.server.username}）")  # noqa: T201

    host = args.host or ("0.0.0.0" if args.lan else settings.server.host)
    start_port = args.port or settings.server.port

    try:
        port = find_free_port(host, start_port)
    except RuntimeError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)  # noqa: T201
        return 1

    if port != start_port:
        print(f"提示：端口 {start_port} 已被占用，已自动改用 {port}。")  # noqa: T201

    # 把最终生效的监听地址回写配置单例。
    # 必要性：应用自身的 lifespan 会读 settings.server.host/port 打启动日志，
    # 若不回写就会出现"命令行说 5111、应用日志说 5000"的误导信息。
    # 注意：--reload 模式下 uvicorn 会另起子进程重新导入配置，此回写不生效——
    # 那只影响开发模式下的日志文案，不影响实际监听（uvicorn 自己持有 port）。
    settings.server.host = host
    settings.server.port = port

    # 实际访问地址：0.0.0.0 不能直接访问，需换成回环地址或提示用本机 IP
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"

    print("=" * 62)  # noqa: T201
    print(f"  {settings.app.name} v{settings.app.version}")  # noqa: T201
    print(f"  访问地址：{url}")  # noqa: T201
    if host in {"0.0.0.0", "::"}:
        print(f"  局域网可访问：已绑定 {host}（登录鉴权已{'开启' if settings.server.auth_required else '关闭'}）")  # noqa: T201
    print(f"  存储目录：{Path('storage').resolve()}")  # noqa: T201
    print("  停止服务：Ctrl + C")  # noqa: T201
    print("=" * 62)  # noqa: T201

    if not args.no_browser and not args.reload:
        open_browser_later(url)

    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=args.reload,
            log_level=args.log_level,
            # WebSocket 空闲时不能主动断开：SSH 终端可能长时间无输出
            ws_ping_interval=None,
            ws_ping_timeout=None,
            # 上传大文件可能耗时较久，放宽 keep-alive
            timeout_keep_alive=75,
            workers=1 if args.reload else args.workers,
        )
    except KeyboardInterrupt:
        print("\n已停止。")  # noqa: T201
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
