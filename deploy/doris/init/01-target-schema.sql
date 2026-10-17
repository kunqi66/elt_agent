-- 演示目标库（profile: demo）——PRD §14.1 的宽表落点。
--
-- 这里**只建正式表**，影子表与错误表由 ``target.ensure_tables`` 在 Commit 那一刻建：
--   * 影子表必须与正式表同形，所以它的正确来源是 ``CREATE TABLE ... LIKE 正式表``，
--     而不是在初始化脚本里把同一份列清单抄第二遍（两份迟早分叉，而分叉只在
--     发布那一步以「列形态不一致」暴露——前面全绿）；
--   * 影子表名带版本号，是运行期事实，静态脚本写不出来。
--
-- 表的形状要能被 demo 源库的 ``biz_demo.orders`` 逐列同名映射（be-10 的确定性起草
-- 按「源列名 = 目标列名」成对），所以列名与顺序刻意与源表一致。

CREATE DATABASE IF NOT EXISTS dw_demo;

-- Doris 的 DUPLICATE KEY 列必须排在列清单最前且顺序一致：orders 的粒度是订单，
-- 同一 order_id 允许多行（演示数据里没有重复，但表语义是明细表而不是主键表）。
-- 用 DUPLICATE 而不是 UNIQUE 的理由：源库的 orders 是「可更新」的，而 Doris 的
-- UNIQUE 表要求写入的是完整行；本次同步是全列同步，两种都能跑，但 DUPLICATE
-- 在列数变化时不会把一次写错变成一条被静默吞掉的行（按 key 去重）。
CREATE TABLE IF NOT EXISTS dw_demo.orders_wide (
    `order_id`   BIGINT        NOT NULL,
    `user_id`    BIGINT        NOT NULL,
    `order_no`   VARCHAR(64)   NOT NULL,
    `amount`     DECIMAL(18,2) NOT NULL,
    `currency`   VARCHAR(8)    NOT NULL,
    `status`     VARCHAR(24)   NOT NULL,
    `channel`    VARCHAR(24)   NULL,
    `remark`     VARCHAR(255)  NULL,
    `created_at` DATETIME      NOT NULL,
    `updated_at` DATETIME      NOT NULL
) ENGINE=OLAP
DUPLICATE KEY(`order_id`)
COMMENT '订单宽表：MySQL 增量同步落点，发布方式是影子表 + 原子 Swap'
DISTRIBUTED BY HASH(`order_id`) BUCKETS 1
PROPERTIES (
    -- 单机 all-in-one 只有 1 个 BE：副本数写 3 会让建表直接失败（"tablet create failed"）
    "replication_num" = "1"
);
