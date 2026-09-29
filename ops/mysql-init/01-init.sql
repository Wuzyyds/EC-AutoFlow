-- ============================================================
-- EC-AutoFlow MySQL 初始化
-- 仅在容器首次创建数据卷时执行一次
-- ============================================================

-- 显式锁定字符集与排序规则，防止镜像版本差异导致默认值不同
CREATE DATABASE IF NOT EXISTS `ec_autoflow`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

ALTER DATABASE `ec_autoflow`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

-- 确认时区为 UTC（全系统统一，TDD-01 原则 4）
-- 若 mysql 库中无时区表，至少保证 NOW() 返回 UTC
SET GLOBAL time_zone = '+00:00';

-- 应用账号权限（官方镜像已创建 MYSQL_USER，此处补齐 DDL 权限，
-- 因为 Alembic 迁移需要 CREATE/ALTER/DROP）
GRANT ALL PRIVILEGES ON `ec_autoflow`.* TO 'ecflow'@'%';
FLUSH PRIVILEGES;

-- 记录初始化基线，便于排障时确认
SELECT
    VERSION()                       AS mysql_version,
    @@character_set_server          AS charset,
    @@collation_server              AS collation,
    @@time_zone                     AS time_zone,
    @@sql_mode                      AS sql_mode;
