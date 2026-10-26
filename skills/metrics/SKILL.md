---
name: metrics
description: 电商指标口径查询技能（GMV/订单量/支付转化率/客单价），涉及指标计算时先加载本技能
allowedTools: [list_tables, query_sql]
---

# 指标口径查询

用户问题涉及下方口径清单中的任一指标时，按以下规则执行：

1. 拆分 query_sql 子问题时只写指标名（如「查询 GMV」），不要自行指定表名
2. 责任表由口径定义唯一决定，禁止把查询换成其它表
3. 查询前先调 list_tables 确认责任表当前已加载；责任表缺失时告知用户数据未就绪

详见 references/metrics.md