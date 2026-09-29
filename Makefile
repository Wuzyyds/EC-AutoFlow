# ============================================================
# EC-AutoFlow 统一命令入口
# 约定：所有开发动作必须通过 Makefile（TDD-01 §4.4）
#       不允许临时敲命令 —— 临时命令不记录、不复用、无法追溯
# ============================================================

SHELL := /bin/bash

# Windows 的 venv 布局是 Scripts/，类 Unix 是 bin/
ifeq ($(OS),Windows_NT)
    VENV_PY  := .venv/Scripts/python.exe
    VENV_PIP := .venv/Scripts/pip.exe
else
    VENV_PY  := .venv/bin/python
    VENV_PIP := .venv/bin/pip
endif

# 允许用系统 Python 创建 venv（首次安装时 venv 还不存在）
BOOTSTRAP_PY ?= python
PY      := $(VENV_PY)
ALEMBIC := $(PY) -m alembic

.DEFAULT_GOAL := help

.PHONY: help
help: ## 显示所有可用命令
	@echo ""
	@echo "EC-AutoFlow 可用命令："
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ------------------------------------------------------------
# 环境准备
# ------------------------------------------------------------

.PHONY: venv
venv: ## 创建虚拟环境
	$(BOOTSTRAP_PY) -m venv .venv
	$(VENV_PY) -m pip install --upgrade pip

.PHONY: install
install: ## 安装依赖（含 dev）
	$(VENV_PY) -m pip install -e ".[dev]"

.PHONY: setup
setup: venv install ## 一键初始化开发环境
	@echo "环境初始化完成。下一步：make db-init && make migrate"

# ------------------------------------------------------------
# 容器编排（可选：本地已有 MySQL 时可跳过）
# ------------------------------------------------------------

.PHONY: up
up: ## 启动容器（MySQL 3307 + Redis）
	docker compose up -d
	@echo "等待 MySQL 就绪..."
	@docker compose exec -T mysql sh -c 'until mysqladmin ping -h127.0.0.1 -uroot -p$$MYSQL_ROOT_PASSWORD --silent; do sleep 1; done'
	@echo "MySQL 已就绪"

.PHONY: down
down: ## 停止容器（保留数据）
	docker compose down

.PHONY: down-hard
down-hard: ## 停止容器并删除数据卷（危险）
	docker compose down -v

.PHONY: logs
logs: ## 查看容器日志
	docker compose logs -f --tail=100

# ------------------------------------------------------------
# 数据库
# ------------------------------------------------------------

.PHONY: db-init
db-init: ## 初始化数据库与应用账号
	$(PY) ops/db_init.py

.PHONY: db-check
db-check: ## 校验数据库能力（版本/字符集/时区/约束有效性）
	$(PY) ops/healthcheck.py

.PHONY: migrate
migrate: ## 执行迁移到最新
	$(ALEMBIC) upgrade head

.PHONY: migrate-down
migrate-down: ## 回滚一个版本
	$(ALEMBIC) downgrade -1

.PHONY: revision
revision: ## 生成迁移脚本（用法：make revision m="描述"）
	$(ALEMBIC) revision --autogenerate -m "$(m)"

.PHONY: migrate-history
migrate-history: ## 查看迁移历史
	$(ALEMBIC) history --verbose

.PHONY: constraints
constraints: ## 生成约束 DDL（CHECK 或触发器，按 MySQL 版本自适应）
	$(PY) ops/generate_constraints.py --apply

.PHONY: seed
seed: ## 写入种子数据（角色、权限、默认规则）
	$(PY) ops/seed.py

.PHONY: reset-db
reset-db: ## 重建数据库（危险：清空所有数据）
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head
	$(PY) ops/seed.py

# ------------------------------------------------------------
# 代码质量
# ------------------------------------------------------------

.PHONY: lint
lint: ## Ruff 检查
	$(PY) -m ruff check .

.PHONY: fmt
fmt: ## Ruff 格式化
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

.PHONY: typecheck
typecheck: ## mypy 类型检查
	$(PY) -m mypy .

.PHONY: check
check: lint typecheck ## 静态检查（lint + 类型）

# ------------------------------------------------------------
# 测试
# ------------------------------------------------------------

.PHONY: test
test: ## 运行全部测试
	$(PY) -m pytest

.PHONY: test-unit
test-unit: ## 只跑单元测试（必须极快）
	$(PY) -m pytest -m unit

.PHONY: test-integration
test-integration: ## 只跑集成测试（需要数据库）
	$(PY) -m pytest -m integration

.PHONY: test-contract
test-contract: ## 适配器契约测试
	$(PY) -m pytest -m contract

.PHONY: cov
cov: ## 测试并输出覆盖率
	$(PY) -m pytest --cov=. --cov-report=term-missing --cov-report=html

.PHONY: ci
ci: check test ## CI 本地预演

# ------------------------------------------------------------
# 运行服务
# ------------------------------------------------------------

.PHONY: run-api
run-api: ## 启动 API 服务（热重载）
	$(PY) -m uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: run-worker
run-worker: ## 启动 Celery worker
	$(PY) -m celery -A apps.worker.celery_app worker -l info -Q default,sync,listing

.PHONY: run-beat
run-beat: ## 启动 Celery beat（定时任务）
	$(PY) -m celery -A apps.worker.celery_app beat -l info

.PHONY: run-flower
run-flower: ## 启动 Celery 监控面板
	$(PY) -m celery -A apps.worker.celery_app flower --port=5555

# ------------------------------------------------------------
# 运维
# ------------------------------------------------------------

.PHONY: status
status: ## 系统状态总览
	$(PY) ops/status.py

.PHONY: backup
backup: ## 手动备份数据库
	$(PY) ops/backup.py

.PHONY: clean
clean: ## 清理缓存产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage 2>/dev/null || true
	@echo "已清理"
