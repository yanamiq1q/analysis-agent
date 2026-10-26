# 电商指标口径定义（stdw 库，ODS 层）

## gmv
- 别名: 销售额 / 成交额 / 交易额 / gmv
- 责任表: ods_order_info
- 口径SQL: SELECT SUM(amount) AS gmv FROM ods_order_info
- 说明: GMV 统计口径：按下单金额计入（含未支付订单）。

## order_cnt
- 别名: 订单量 / 订单数 / 下单量 / 成交单量
- 责任表: ods_order_info
- 口径SQL: SELECT COUNT(*) AS order_cnt FROM ods_order_info
- 说明: 订单量口径：按订单主表行数统计，一笔订单记 1 次。

## pay_order_cnt
- 别名: 支付订单数 / 支付订单量 / 成交订单数
- 责任表: ods_order_info
- 口径SQL: SELECT COUNT(*) AS pay_order_cnt FROM ods_order_info WHERE status = 'paid'
- 说明: 支付订单口径：status='paid' 的订单，区别于下单量。

## pay_convert_rate
- 别名: 支付转化率 / 成交转化率 / 转化率
- 责任表: ods_order_info
- 口径SQL: SELECT SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS pay_convert_rate FROM ods_order_info
- 说明: 支付转化率口径：支付订单数 / 总下单数（小数，非百分数）。

## avg_order_amount
- 别名: 客单价 / 人均消费 / arpu
- 责任表: ods_order_info
- 口径SQL: SELECT SUM(amount) * 1.0 / COUNT(DISTINCT user_id) AS avg_order_amount FROM ods_order_info
- 说明: 客单价口径：销售总额 / 去重用户数（按人平均，不按单平均）。