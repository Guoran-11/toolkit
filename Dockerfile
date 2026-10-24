# ============================================================================
# 全能工具站（ToolKit）—— 通用镜像
#
# 多阶段构建：
#   stage builder —— 仅负责编译/安装 Python 依赖，产物与源码分离
#   stage runtime —— 只带运行时所需内容，镜像更小、攻击面更窄
#
# 重要：本镜像**默认不含** LibreOffice / FFmpeg / Chromium —— 三者体积合计
# 超过 700MB。需要 Office / HTML→PDF / 音频转码能力时，选择下面任一方案：
#   A. 构建时加参数：docker build --build-arg WITH_ENGINES=1 .
#   B. 复用宿主已安装的引擎：给容器挂载卷并改 config.toml 的 engines 路径
#   C. 用 debian 完整版镜像（见文末注释）
# 缺失引擎时，对应功能返回带安装指引的 503，服务本身仍可正常启动。
# ============================================================================

# ---------------------------------------------------------------- 构建阶段
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 先只拷依赖清单，最大化利用 Docker 层缓存
COPY requirements.txt requirements-core.txt ./

# 使用 venv 承载依赖，方便整目录拷入运行阶段，且不带编译器
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------- 运行阶段
FROM python:3.12-slim AS runtime

ARG WITH_ENGINES=0
ARG DEBIAN_FRONTEND=noninteractive

# 基础运行库：
#   fonts-noto-cjk —— HTML→PDF / Office→PDF 的中文字体，缺了会变成方块
#   libgl1 / libglib2.0-0 —— PyMuPDF 与 Chromium 的运行时依赖
#   poppler-utils —— pdf2docx / pdfplumber 的部分后端需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-noto-cjk \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# WITH_ENGINES=1 时安装 LibreOffice 与 FFmpeg（镜像会显著变大）
RUN if [ "$WITH_ENGINES" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libreoffice-writer libreoffice-calc libreoffice-impress \
            ffmpeg \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# 依赖从构建阶段整目录拷入。**必须早于 playwright install**：
# 否则 /opt/venv/bin/python 尚不存在，Chromium 那一层会失败。
COPY --from=builder /opt/venv /opt/venv

# Playwright/Chromium 单独一层，便于缓存（约 150MB）。
# 这里刻意**不**再吞掉输出与错误：历史上它带着 `2>/dev/null || true` 且位于
# COPY 之前，结果是镜像里永远没有 Chromium，HTML→PDF / Markdown 导出在容器中
# 完全不可用却毫无提示。安装失败应当让构建失败。
RUN /opt/venv/bin/python -m playwright install --with-deps chromium

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOOLKIT_HOST=0.0.0.0 \
    TOOLKIT_PORT=5000 \
    # 容器内必须开鉴权，否则任何能访问端口的人都能用
    TOOLKIT_AUTH_REQUIRED=true

WORKDIR /app
COPY app/ ./app/
COPY run.py config.toml ./

# storage 与 runtime 以非 root 用户可写
RUN useradd --create-home --shell /usr/sbin/nologin toolkit \
    && mkdir -p /app/storage/{uploads,temp,outputs,logs} /app/runtime \
    && chown -R toolkit:toolkit /app

USER toolkit

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz',timeout=3).status==200 else 1)"

# 单进程 + 内部线程池即可：任务是 CPU/IO 混合型，多 worker 会争抢 LibreOffice
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "5000", "--no-browser"]

# ----------------------------------------------------------------------------
# 方案 C：需要完整能力时，把 FROM python:3.12-slim 换成 debian 并直接 apt 安装。
# 示例（不推荐用于生产，仅便于本地验证）：
#   FROM python:3.12
#   RUN apt-get update && apt-get install -y libreoffice ffmpeg fonts-noto-cjk
# ----------------------------------------------------------------------------
