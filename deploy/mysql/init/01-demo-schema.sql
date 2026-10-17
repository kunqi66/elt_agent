-- 演示源库（profile: demo）——对应 PRD §14.1「MySQL 至 Doris 业务宽表增量同步」
-- 刻意保留敏感字段（手机号、身份证、邮箱）用于验证「脱敏探查」与「敏感明文不出域」。

CREATE DATABASE IF NOT EXISTS biz_demo DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE biz_demo;

-- 订单表
CREATE TABLE IF NOT EXISTS orders (
    order_id      BIGINT       NOT NULL AUTO_INCREMENT,
    user_id       BIGINT       NOT NULL,
    order_no      VARCHAR(64)  NOT NULL,
    amount        DECIMAL(18,2) NOT NULL,
    currency      VARCHAR(8)    NOT NULL DEFAULT 'CNY',
    status        VARCHAR(24)   NOT NULL,
    channel       VARCHAR(24)   NULL,
    remark        VARCHAR(255)  NULL,
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id),
    UNIQUE KEY uk_orders_order_no (order_no),
    KEY idx_orders_updated_at (updated_at),
    KEY idx_orders_user_id (user_id)
) ENGINE=InnoDB;

-- 用户表（含敏感字段）
CREATE TABLE IF NOT EXISTS users (
    user_id       BIGINT       NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    real_name     VARCHAR(64)  NULL,
    phone         VARCHAR(20)  NULL,          -- 敏感：手机号
    id_card       VARCHAR(20)  NULL,          -- 敏感：身份证
    email         VARCHAR(128) NULL,          -- 敏感：邮箱
    address       VARCHAR(255) NULL,          -- 敏感：地址
    city          VARCHAR(64)  NULL,
    register_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    UNIQUE KEY uk_users_username (username),
    KEY idx_users_updated_at (updated_at)
) ENGINE=InnoDB;

-- ---------------- 演示数据 ----------------
INSERT INTO users (username, real_name, phone, id_card, email, address, city, register_at)
VALUES
    ('alice',  '张小雨', '13800000001', '110101199001011234', 'alice@example.com',  '北京市朝阳区某某路 1 号', '北京', '2025-01-05 10:00:00'),
    ('bob',    '李大山', '13900000002', '310101199202022345', 'bob@example.com',    '上海市浦东新区某某路 2 号', '上海', '2025-02-11 14:30:00'),
    ('carol',  '王小明', '13700000003', '440101199303033456', 'carol@example.com',  '广州市天河区某某路 3 号', '广州', '2025-03-20 09:15:00'),
    ('dave',   '赵晓峰', '13600000004', '510101199404044567', 'dave@example.com',   '成都市武侯区某某路 4 号', '成都', '2025-04-02 16:45:00'),
    ('erin',   '陈静',   '13500000005', '320101199505055678', 'erin@example.com',   '南京市鼓楼区某某路 5 号', '南京', '2025-05-18 11:20:00');

INSERT INTO orders (user_id, order_no, amount, currency, status, channel, remark, created_at, updated_at)
VALUES
    (1, 'SO2026010001',  199.00, 'CNY', 'PAID',      'app',    NULL,           '2026-01-02 08:30:00', '2026-01-02 08:30:00'),
    (1, 'SO2026010002',  899.50, 'CNY', 'PAID',      'web',    NULL,           '2026-01-03 12:10:00', '2026-01-03 12:10:00'),
    (2, 'SO2026010003',   45.80, 'CNY', 'REFUNDED',  'app',    '客户申请退款',  '2026-01-05 19:05:00', '2026-01-06 09:00:00'),
    (2, 'SO2026010004', 2380.00, 'CNY', 'PAID',      'web',    NULL,           '2026-01-08 21:40:00', '2026-01-08 21:40:00'),
    (3, 'SO2026010005',  560.00, 'CNY', 'PENDING',   'miniapp', NULL,          '2026-01-11 10:25:00', '2026-01-11 10:25:00'),
    (3, 'SO2026010006',  120.00, 'CNY', 'PAID',      'app',    NULL,           '2026-01-12 15:55:00', '2026-01-12 15:55:00'),
    (4, 'SO2026010007', 3600.00, 'CNY', 'PAID',      'web',    '企业采购',      '2026-01-15 09:35:00', '2026-01-15 09:35:00'),
    (5, 'SO2026010008',   78.90, 'CNY', 'CANCELLED', 'app',    '超时未支付',    '2026-01-18 20:00:00', '2026-01-19 08:00:00'),
    (1, 'SO2026010009',  999.00, 'CNY', 'PAID',      'app',    NULL,           '2026-01-20 13:15:00', '2026-01-20 13:15:00'),
    -- 故意制造脏数据：金额为负 / 状态非法值 —— 用于触发质量契约的错误分流（演示场景二）
    (2, 'SO2026010010',  -50.00, 'CNY', 'PAID',      'app',    '异常金额',      '2026-01-21 07:45:00', '2026-01-21 07:45:00'),
    (3, 'SO2026010011',  300.00, 'CNY', 'UNKNOWN',   'web',    '非法状态',      '2026-01-22 18:20:00', '2026-01-22 18:20:00');

-- 演示账号仅用于源库连接（凭据应写入 Vault，不落库明文）
GRANT SELECT ON biz_demo.* TO 'etl_demo'@'%';
FLUSH PRIVILEGES;
