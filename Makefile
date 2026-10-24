# ============================================================================
# 全能工具站（ToolKit）—— 常用命令
#
# 用法： make <target>
#   在没有 make 的 Windows 上，直接照抄下面命令行的部分手工执行即可。
# ============================================================================

# 虚拟环境与解释器。可通过 `make PY=/path/to/python dev` 覆盖。
PY        ?= python
VENV      ?= .venv
VENV_PY   := $(VENV)/bin/python
HOST      ?= 127.0.0.1
PORT      ?= 5000

.DEFAULT_GOAL := help

# ---------------------------------------------------------------- 帮助

.PHONY: help
help: ## 显示全部可用目标
	@echo "全能工具站 —— 可用命令："
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- 环境

.PHONY: venv
venv: ## 创建虚拟环境
	$(PY) -m venv $(VENV)
	@echo "已创建 $(VENV)。下一步： make install"

.PHONY: install
install: ## 安装全部依赖（含各转换引擎 Python 库）
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt

.PHONY: install-core
install-core: ## 只装核心依赖（服务可启动，引擎缺失时功能降级）
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements-core.txt

.PHONY: install-dev
install-dev: install ## 额外安装开发/测试依赖
	$(VENV_PY) -m pip install -r requirements.txt pytest pytest-asyncio httpx ruff

.PHONY: browsers
browsers: ## 下载 Playwright 的 Chromium（HTML→PDF / Markdown 导出所需）
	$(VENV_PY) -m playwright install chromium

# ---------------------------------------------------------------- 运行

.PHONY: run
run: ## 启动服务（仅本机）
	$(VENV_PY) run.py --host $(HOST) --port $(PORT)

.PHONY: run-lan
run-lan: ## 启动服务并暴露到局域网（自动强制开启鉴权）
	$(VENV_PY) run.py --lan --port $(PORT)

.PHONY: dev
dev: ## 开发模式（代码热重载）
	$(VENV_PY) run.py --reload --port $(PORT)

# ---------------------------------------------------------------- 质量

.PHONY: test
test: ## 运行测试
	$(VENV_PY) -m pytest -q

.PHONY: test-v
test-v: ## 运行测试（详细输出）
	$(VENV_PY) -m pytest -v

.PHONY: cov
cov: ## 运行测试并生成覆盖率报告
	$(VENV_PY) -m pytest --cov=app --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## 静态检查
	$(VENV_PY) -m ruff check app tests run.py

.PHONY: fmt
fmt: ## 格式化代码
	$(VENV_PY) -m ruff format app tests run.py

.PHONY: fix
fix: ## 自动修复可修复的检查问题
	$(VENV_PY) -m ruff check --fix app tests run.py

.PHONY: check
check: lint test ## 提交前的完整检查

# ---------------------------------------------------------------- 部署

.PHONY: docker-build
docker-build: ## 构建镜像（不含 LibreOffice/FFmpeg）
	docker build -t toolkit:1.0.0 .

.PHONY: docker-build-full
docker-build-full: ## 构建镜像（含 LibreOffice + FFmpeg，体积大）
	docker build --build-arg WITH_ENGINES=1 -t toolkit:1.0.0-full .

.PHONY: up
up: ## 启动容器（后台）
	docker compose up -d --build

.PHONY: down
down: ## 停止容器
	docker compose down

.PHONY: logs
logs: ## 跟踪容器日志
	docker compose logs -f toolkit

# ---------------------------------------------------------------- 维护

.PHONY: clean
clean: ## 清理 Python 缓存与测试产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	@echo "已清理缓存"

.PHONY: clean-storage
clean-storage: ## ⚠️ 清空 storage（上传/产物/任务表全部删除）
	@echo "⚠️  将删除 storage/ 下的全部数据，3 秒后执行，Ctrl+C 取消…"
	@sleep 3
	rm -rf storage/uploads/* storage/temp/* storage/outputs/* storage/tasks.json 2>/dev/null || true
	@echo "storage 已清空"
