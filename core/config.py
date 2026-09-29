"""分层配置。

配置分四层（TDD-05 §1.1）：

| 层 | 存放位置 | 修改方式 | 适用内容 |
|---|---|---|---|
| L1 环境配置 | 环境变量 / `.env` | 改配置 + 重启 | 连接串、密钥、外部端点 |
| L2 部署配置 | 随镜像的配置文件 | 发版 | 功能开关、日志级别、并发数 |
| L3 业务配置 | **数据库表** | 界面修改，实时生效 | 审批阈值、预警规则、成本口径 |
| L4 数据配置 | 数据库表 | 业务录入 | 成本项、汇率、SKU 映射 |

**本模块只承载 L1。** 业务参数（审批金额上限、毛利红线）一律进数据库，
不要写在这里 —— 否则改一个阈值就要重启服务。

MySQL 连接补偿（ADR-007 §3.3、§6）：
    本机 MySQL 服务端时区是 SYSTEM、sql_mode 非严格，
    这些都会导致数据静默错误。因此连接串强制注入：
      - time_zone='+00:00'          → 统一 UTC
      - sql_mode=STRICT_...         → 拒绝隐式截断
      - transaction_isolation=READ-COMMITTED  → 与设计假设一致
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.exceptions import ConfigurationError

__all__ = ["Settings", "get_settings", "reset_settings_cache", "BASE_DIR", "EnvName"]

BASE_DIR = Path(__file__).resolve().parent.parent

EnvName = Literal["local", "test", "sandbox", "staging", "prod"]

#: 强制注入的连接级参数。这是 MySQL 适配的关键补偿，不可省略。
MYSQL_INIT_COMMAND = (
    "SET time_zone='+00:00', "
    "sql_mode='STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION', "
    "transaction_isolation='READ-COMMITTED'"
)

#: 允许出网的环境。test 环境必须禁止出网（TDD-01 §4.1），
#: 否则 CI 会因网络抖动随机失败，最终团队开始忽略失败。
NETWORK_ALLOWED_ENVS: frozenset[str] = frozenset({"local", "sandbox", "staging", "prod"})


class Settings(BaseSettings):
    """全局配置。

    字段名与 `.env` 中的大写变量一一对应（pydantic-settings 不区分大小写）。
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 运行环境 ----------
    ec_env: EnvName = "local"
    ec_debug: bool = False

    # ---------- MySQL ----------
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "ec_autoflow"
    mysql_user: str = "ecflow"
    mysql_password: str = "ecflow_dev_pwd"
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 20
    mysql_pool_recycle_seconds: int = 1800  # 防 MySQL 8 小时空闲断连
    mysql_echo: bool = False

    # ---------- Redis ----------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # ---------- 加密 ----------
    ec_master_key: str = ""
    ec_master_key_version: int = 1

    # ---------- 平台凭据 ----------
    amazon_lwa_client_id: str = ""
    amazon_lwa_client_secret: str = ""
    amazon_ads_client_id: str = ""
    amazon_ads_client_secret: str = ""
    tiktok_app_key: str = ""
    tiktok_app_secret: str = ""

    # ---------- LLM ----------
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 30

    # ---------- 通知 ----------
    wecom_webhook_url: str = ""
    feishu_webhook_url: str = ""

    # ---------- 对象存储 ----------
    storage_endpoint: str = ""
    storage_bucket: str = "ec-autoflow"
    storage_access_key: str = ""
    storage_secret_key: str = ""

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_json: bool = True

    # ========================================================
    # 派生属性
    # ========================================================

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_test(self) -> bool:
        return self.ec_env == "test"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_prod(self) -> bool:
        return self.ec_env == "prod"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def network_allowed(self) -> bool:
        """是否允许发起真实网络请求。

        test 环境返回 False，适配器工厂据此强制使用 Mock。
        """
        return self.ec_env in NETWORK_ALLOWED_ENVS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """异步连接串（API 层用）。"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """同步连接串（Celery worker 用）。

        ADR-002：API 层全异步，worker 层同步。
        """
        return (
            f"mysql+pymysql://{self.mysql_user}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_safe(self) -> str:
        """脱敏的连接串，用于日志输出。"""
        return (
            f"mysql://{self.mysql_user}:***"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def db_connect_args(self) -> dict[str, object]:
        """**异步**引擎（aiomysql）的连接参数。

        **这是 MySQL 适配的关键补偿**（ADR-007 §3.3）：
        本机 MySQL 的 time_zone 是 SYSTEM、sql_mode 非严格，
        不注入这些参数会导致时间语义混乱与数据静默截断。

        三个注意点：
        1. **不传 charset** —— 它已由 URL 的 `?charset=utf8mb4` 提供。
           两处都传会让 SQLAlchemy 抛
           `TypeError: got multiple values for keyword argument 'charset'`。
        2. **不传 read_timeout / write_timeout** —— aiomysql 的 connect()
           没有这两个参数（那是 pymysql 的），传了会
           `TypeError: connect() got an unexpected keyword argument`。
           同步引擎用 db_connect_args_sync 单独提供。
        3. **不传 autocommit** —— 事务由 SQLAlchemy Session 统一管理，
           驱动层再设一次会产生两套事务语义。
        """
        return {
            "init_command": MYSQL_INIT_COMMAND,
            "connect_timeout": 10,
        }

    @property
    def db_connect_args_sync(self) -> dict[str, object]:
        """**同步**引擎（pymysql）的连接参数。

        pymysql 支持读写超时，加上它们可以避免长查询把 worker 挂死。
        """
        return {
            **self.db_connect_args,
            "read_timeout": 120,
            "write_timeout": 120,
        }

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return f"{self.redis_url.rstrip('/')}/{self.redis_db + 1}"

    # ========================================================
    # 启动期校验
    # ========================================================

    @model_validator(mode="after")
    def validate_environment_safety(self) -> Settings:
        """环境安全校验。

        这类错误必须**阻止服务启动** ——
        带着错误配置运行，比不运行危险得多（会产生错误数据且无人察觉）。
        """
        if self.ec_env == "prod":
            if self.ec_debug:
                raise ConfigurationError(
                    "生产环境禁止开启 debug",
                    code="PROD_DEBUG_FORBIDDEN",
                    action="将 EC_DEBUG 设为 false",
                )
            if not self.ec_master_key:
                raise ConfigurationError(
                    "生产环境必须配置加密主密钥 EC_MASTER_KEY",
                    code="PROD_MASTER_KEY_MISSING",
                    action="从 KMS 或密钥管理服务注入 EC_MASTER_KEY",
                )
            if self.mysql_password in {"root", "123456", "password", ""}:
                raise ConfigurationError(
                    "生产环境禁止使用弱数据库密码",
                    code="PROD_WEAK_DB_PASSWORD",
                    action="改用高强度随机密码",
                )
            if self.mysql_echo:
                raise ConfigurationError(
                    "生产环境禁止开启 SQL 回显（会记录全部 SQL 与参数，含 PII）",
                    code="PROD_SQL_ECHO_FORBIDDEN",
                    action="将 MYSQL_ECHO 设为 false",
                )

        if self.ec_env == "test" and self.log_json:
            # test 环境用可读日志更便于排障
            object.__setattr__(self, "log_json", False)

        if not 1 <= self.mysql_port <= 65535:
            raise ConfigurationError(
                f"MySQL 端口非法：{self.mysql_port}",
                code="INVALID_DB_PORT",
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。

    用 lru_cache 保证全局唯一 —— 配置对象被多处引用，
    如果每次 new 一个，会出现"同一进程内两个不同配置"的诡异问题。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清空配置缓存。仅测试使用（切换环境变量后需要重新加载）。"""
    get_settings.cache_clear()
