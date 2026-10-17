-- 演示目标库（MySQL 形态，profile: demo）。
--
-- 为什么同一套演示同时保留 MySQL 与 Doris 两个目标端：这两个是本项目**仅有的**
-- 两个已实现的目标端（``clients/target/{mysql,doris}.py``），而它们的发布方式在
-- 平台层是不同的（MySQL 用单条 ``RENAME TABLE`` 三步交换，Doris 用 ``swap=true``），
-- 对账逻辑共用 ``clients/target/engine.py`` 一份。演示默认走 **Doris**（PRD §14.1）；
-- MySQL 目标端用于「Doris 起不来（ECS 内存不足）时仍能跑通全链」这条退路，
-- 以及验证「换一个目标端只换一个实现类」这件事。
--
-- 与 Doris 侧的 ``deploy/doris/init/01-target-schema.sql`` 保持**同名同列**：
-- 目标端换掉而表名不变，才能让同一条需求在两边都指向 ``dw_demo.orders_wide``。

CREATE DATABASE IF NOT EXISTS dw_demo DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE dw_demo;

-- 列名与顺序刻意与源库 ``biz_demo.orders`` 一致：be-10 的确定性起草按
-- 「源列名 = 目标列名」成对，列名不一致时会在门禁的 check_schema 上被拦下。
CREATE TABLE IF NOT EXISTS orders_wide (
    order_id   BIGINT        NOT NULL,
    user_id    BIGINT        NOT NULL,
    order_no   VARCHAR(64)   NOT NULL,
    amount     DECIMAL(18,2) NOT NULL,
    currency   VARCHAR(8)    NOT NULL,
    status     VARCHAR(24)   NOT NULL,
    channel    VARCHAR(24)   NULL,
    remark     VARCHAR(255)  NULL,
    created_at DATETIME      NOT NULL,
    updated_at DATETIME      NOT NULL,
    PRIMARY KEY (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 目标端账号需要 **DDL 权限**：影子表由 ``target.ensure_tables`` 建（CREATE TABLE ... LIKE），
-- 发布与回滚是 ``RENAME TABLE``。只给 SELECT/INSERT 的话，链路会在提交那一刻
-- 以「CREATE command denied」失败——而报错出现在目标端，读起来像权限没配好，
-- 实际是「目标端账号本来就该有 DDL 权限」这件事没写下来过。
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES ON dw_demo.* TO 'etl_demo'@'%';
FLUSH PRIVILEGES;
