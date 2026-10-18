-- ============================================================
-- stdw 建库脚本：电商订单实时数仓演示库
-- ------------------------------------------------------------
-- 用途   : data-agent 项目 "MySQL 直连 + 指标字典联动" 验证库
-- 分层   : ODS 7 / DIM 4 / DWD 3 / DWS 2 / ADS 1 = 17 张表
-- 关键表 : ods_order_info（对齐 data/metrics.py 指标口径的依赖表：
--          gmv / 订单量 / 支付订单数 / 支付转化率 / 客单价）
-- 执行   : mysql -uroot -p < scripts/stdw_init.sql
-- 重建   : 2026-10-17（原数据目录失效后重建，行数与口径对应当日验证）
-- ============================================================

-- 可选：root 密码重置（与项目验证脚本保持一致）——按需取消注释
-- ALTER USER 'root'@'localhost' IDENTIFIED BY 'wuqiru570';
-- FLUSH PRIVILEGES;

CREATE DATABASE IF NOT EXISTS stdw DEFAULT CHARSET utf8mb4;
USE stdw;

-- ==================== ODS 层：贴源原始数据（7 张） ====================

-- 订单主表 ★ 指标字典依赖表，字段必须与 metrics.py 对齐
-- (user_id / amount / status)
CREATE TABLE ods_order_info (
    order_id    BIGINT       PRIMARY KEY     COMMENT '订单 ID',
    user_id     BIGINT                       COMMENT '用户 ID',
    amount      DECIMAL(10,2)                COMMENT '下单金额',
    status      VARCHAR(20)                  COMMENT "订单状态 paid/pending",
    create_time DATETIME                     COMMENT '下单时间'
) COMMENT '订单主表（ODS）';

CREATE TABLE ods_order_detail (
    order_id BIGINT        COMMENT '订单 ID',
    sku_id   BIGINT        COMMENT '商品 SKU ID',
    cnt      INT           COMMENT '购买数量',
    price    DECIMAL(10,2) COMMENT '成交单价'
) COMMENT '订单明细表（ODS）';

CREATE TABLE ods_payment_info (
    payment_id BIGINT       PRIMARY KEY COMMENT '支付流水 ID',
    order_id   BIGINT                     COMMENT '订单 ID',
    pay_amount DECIMAL(10,2)              COMMENT '支付金额',
    pay_time   DATETIME                   COMMENT '支付时间'
) COMMENT '支付流水表（ODS）';

CREATE TABLE ods_user_login (
    user_id    BIGINT   COMMENT '用户 ID',
    login_time DATETIME COMMENT '登录时间',
    ip         VARCHAR(64) COMMENT '登录 IP'
) COMMENT '用户登录日志（ODS）';

CREATE TABLE ods_cart_info (
    user_id  BIGINT COMMENT '用户 ID',
    sku_id   BIGINT COMMENT '商品 SKU ID',
    cnt      INT    COMMENT '加购数量',
    add_time DATETIME COMMENT '加购时间'
) COMMENT '购物车加购明细（ODS）';

CREATE TABLE ods_review_info (
    review_id BIGINT PRIMARY KEY COMMENT '评价 ID',
    order_id  BIGINT              COMMENT '订单 ID',
    score     INT                 COMMENT '评分 1-5',
    content   TEXT                COMMENT '评价内容'
) COMMENT '商品评价表（ODS）';

CREATE TABLE ods_app_log (
    log_id BIGINT PRIMARY KEY COMMENT '日志 ID',
    user_id BIGINT            COMMENT '用户 ID',
    action  VARCHAR(50)       COMMENT '行为类型',
    ts      DATETIME          COMMENT '行为时间'
) COMMENT 'APP 埋点日志（ODS）';

-- ==================== DIM 层：维度表（4 张） ====================

-- 用户维：拉链表（SCD Type 2），start_dt/end_dt/is_current 是拉链三件套
CREATE TABLE dim_user (
    user_id    BIGINT      COMMENT '用户 ID',
    user_name  VARCHAR(50) COMMENT '用户名',
    level      VARCHAR(20) COMMENT '会员等级 Gold/Silver/Bronze',
    start_dt   DATE        COMMENT '当前版本生效日期',
    end_dt     DATE        COMMENT '当前版本失效日期（9999-12-31=有效）',
    is_current TINYINT     COMMENT '是否当前版本 1/0'
) COMMENT '用户维度表（拉链表 SCD2）';

CREATE TABLE dim_product (
    product_id   BIGINT      PRIMARY KEY COMMENT '商品 ID',
    product_name VARCHAR(100)             COMMENT '商品名',
    category     VARCHAR(50)              COMMENT '类目'
) COMMENT '商品维度表';

CREATE TABLE dim_date (
    dt           DATE PRIMARY KEY COMMENT '日期',
    week_of_year INT            COMMENT '年内周数',
    is_holiday   TINYINT        COMMENT '是否节假日 1/0'
) COMMENT '日期维度表';

CREATE TABLE dim_coupon (
    coupon_id   BIGINT      PRIMARY KEY COMMENT '优惠券 ID',
    coupon_name VARCHAR(50)             COMMENT '券名称',
    discount    DECIMAL(5,2)            COMMENT '折扣额'
) COMMENT '优惠券维度表';

-- ==================== DWD 层：明细事实（3 张） ====================

CREATE TABLE dwd_order_fact (
    order_id    BIGINT       PRIMARY KEY COMMENT '订单 ID',
    user_id     BIGINT                     COMMENT '用户 ID',
    amount      DECIMAL(10,2)              COMMENT '下单金额',
    status      VARCHAR(20)                COMMENT '订单状态',
    create_time DATETIME                   COMMENT '下单时间'
) COMMENT '订单明细事实（DWD）';

CREATE TABLE dwd_payment_fact (
    payment_id BIGINT       PRIMARY KEY COMMENT '支付流水 ID',
    order_id   BIGINT                     COMMENT '订单 ID',
    user_id    BIGINT                     COMMENT '用户 ID',
    pay_amount DECIMAL(10,2)              COMMENT '支付金额',
    pay_time   DATETIME                   COMMENT '支付时间'
) COMMENT '支付明细事实（DWD）';

CREATE TABLE dwd_user_behavior (
    user_id BIGINT      COMMENT '用户 ID',
    action  VARCHAR(50) COMMENT '行为类型',
    sku_id  BIGINT      COMMENT '商品 SKU ID',
    ts      DATETIME    COMMENT '行为时间'
) COMMENT '用户行为明细（DWD）';

-- ==================== DWS 层：汇总（2 张） ====================

CREATE TABLE dws_user_order_1d (
    dt        DATE         COMMENT '统计日期',
    user_id   BIGINT       COMMENT '用户 ID',
    order_cnt INT          COMMENT '当日下单数',
    gmv       DECIMAL(12,2) COMMENT '当日成交额'
) COMMENT '用户日粒度下单汇总（DWS）';

CREATE TABLE dws_product_sales_1d (
    dt         DATE         COMMENT '统计日期',
    product_id BIGINT       COMMENT '商品 ID',
    sale_cnt   INT          COMMENT '当日销量',
    gmv        DECIMAL(12,2) COMMENT '当日成交额'
) COMMENT '商品日粒度销售汇总（DWS）';

-- ==================== ADS 层：报表（1 张） ====================

CREATE TABLE ads_order_daily_report (
    dt       DATE         COMMENT '统计日期',
    order_cnt INT          COMMENT '当日订单量',
    gmv      DECIMAL(12,2) COMMENT '当日 GMV',
    pay_rate DECIMAL(6,4)  COMMENT '当日支付转化率（小数）'
) COMMENT '订单日报（ADS）';

-- ==================== 演示数据 ====================

-- ★ 指标验证核心表：1200 行订单，paid 约 70%
-- 用递归 CTE 生成序列，避免手写 1200 条 INSERT（MySQL 8.0+ 语法）
-- MySQL 默认递归上限 1000，1200 行需先在会话级调高
SET SESSION cte_max_recursion_depth = 2000;
INSERT INTO ods_order_info (order_id, user_id, amount, status, create_time)
WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 1200
)
SELECT
    n,
    MOD(n, 300) + 1,                                   -- user_id 1~300 循环
    ROUND(50 + RAND() * 4950, 2),                      -- amount 50~5000
    IF(RAND() < 0.7, 'paid', 'pending'),               -- 约 70% 已支付
    TIMESTAMP(CONCAT('2026-10-',
        LPAD(MOD(n, 16) + 1, 2, '0'), ' ',             -- 日 01~16
        LPAD(MOD(n, 24), 2, '0'), ':00:00'))           -- 时 00~23
FROM seq;

-- 用户维拉链演示数据（10 个用户，各只有一个当前版本）
INSERT INTO dim_user (user_id, user_name, level, start_dt, end_dt, is_current) VALUES
    (1, 'user_1', 'Gold',   '2026-01-01', '9999-12-31', 1),
    (2, 'user_2', 'Silver', '2026-01-01', '9999-12-31', 1),
    (3, 'user_3', 'Gold',   '2026-01-01', '9999-12-31', 1),
    (4, 'user_4', 'Bronze', '2026-01-01', '9999-12-31', 1),
    (5, 'user_5', 'Silver', '2026-01-01', '9999-12-31', 1),
    (6, 'user_6', 'Bronze', '2026-01-01', '9999-12-31', 1),
    (7, 'user_7', 'Gold',   '2026-01-01', '9999-12-31', 1),
    (8, 'user_8', 'Silver', '2026-01-01', '9999-12-31', 1),
    (9, 'user_9', 'Bronze', '2026-01-01', '9999-12-31', 1),
    (10, 'user_10', 'Gold', '2026-01-01', '9999-12-31', 1);

-- DWS：10 个用户 2026-10-15 的下单汇总
INSERT INTO dws_user_order_1d (dt, user_id, order_cnt, gmv) VALUES
    ('2026-10-15', 1, 5, 1500.00), ('2026-10-15', 2, 3, 920.50),
    ('2026-10-15', 3, 8, 2760.00), ('2026-10-15', 4, 2, 610.00),
    ('2026-10-15', 5, 4, 1330.25), ('2026-10-15', 6, 1, 399.00),
    ('2026-10-15', 7, 7, 2410.80), ('2026-10-15', 8, 3, 875.00),
    ('2026-10-15', 9, 2, 540.50), ('2026-10-15', 10, 6, 1980.00);

-- ADS：10-01 ~ 10-16 日报
INSERT INTO ads_order_daily_report (dt, order_cnt, gmv, pay_rate) VALUES
    ('2026-10-01', 60, 82000.00, 0.6900), ('2026-10-02', 72, 95100.00, 0.7100),
    ('2026-10-03', 68, 88400.00, 0.6600), ('2026-10-04', 55, 73100.00, 0.6800),
    ('2026-10-05', 80, 109000.00, 0.7200), ('2026-10-06', 91, 126000.00, 0.7400),
    ('2026-10-07', 102, 141000.00, 0.7000), ('2026-10-08', 88, 120500.00, 0.6900),
    ('2026-10-09', 76, 99800.00, 0.7100), ('2026-10-10', 95, 132000.00, 0.7300),
    ('2026-10-11', 110, 154000.00, 0.7500), ('2026-10-12', 98, 136500.00, 0.7200),
    ('2026-10-13', 85, 112000.00, 0.6800), ('2026-10-14', 79, 103500.00, 0.7000),
    ('2026-10-15', 93, 128000.00, 0.7400), ('2026-10-16', 105, 146000.00, 0.7600);

-- 其余表（ods_order_detail / ods_payment_info 等）结构就绪，
-- 演示数据按需用项目 data-agent 的文件上传或后续 INSERT 填充。

-- 快速自检（最后一项是指标验证基准值）
SELECT 'stdw tests:' AS info;
SELECT COUNT(*) AS ods_order_info_rows FROM ods_order_info;
SELECT COUNT(*) AS paid_rows FROM ods_order_info WHERE status = 'paid';
SELECT SUM(amount) AS total_gmv FROM ods_order_info;