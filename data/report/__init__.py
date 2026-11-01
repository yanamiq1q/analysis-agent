"""报告流水线（PR1）。

把模板 + SQL 变成一份**结构化报告包**（ReportSpec + 图表 + 状态），
交给 WorkBuddy 之类的宿主渲染成 PPT / 飞书文档。

入口：
    python -m data.report.run --template monthly_ecom --period 2026-10
"""
