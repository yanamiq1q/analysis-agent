---
name: report-producer
description: 用 data-agent 生成结构化经营报告包，并制作成 PPT 或飞书文档、可选发到群。当用户要"出周报/月报/经营分析/数据报告"时使用。
---

# 经营报告生产

把"数据"和"呈现"分开：**数字由 data-agent 产出并负责准确，你只负责排版和分发。**

## 第 0 步：先自检（不通过就停）

本技能依赖 4 个 MCP 工具：

| 工具 | 用途 |
|---|---|
| `list_report_templates` | 选模板 |
| `build_report` | 提交任务 |
| `get_report` | 轮询状态 |
| `get_report_spec` | 取报告内容 |

**任意一个看不到，立刻停止**，原样回复用户：

> 当前连接的 data-agent 是旧进程（只有 8 个基础工具）。请关掉它的窗口，
> 重新运行 `start-server.bat`，在 MCP 面板确认工具数 ≥ 12 且能看到
> `build_report`，然后告诉我，我再继续。

然后**什么也别做**。下面四件事都会浪费用户时间或产出假数据：

1. ❌ 不要用 `query_sql` / `ask_data` 自己算一组数字"代替报告"——那会和报告包对不上；
2. ❌ 不要读旧的 `report.json`、旧产物或任何示例来顶替——**旧数据比没数据更危险**；
3. ❌ 不要尝试本地 CLI（`python -m data.report.run`）——服务和 CLI 抢 DuckDB 单进程锁，必然失败；
4. ❌ 不要去查进程启动时间、翻代码、找"代码缺陷"——这是进程过期，重启是唯一解。

## 流程

### 1. 确认两件事（缺了就问，别猜）

- **周期**：`2026-10`（整月）或 `2026-10-01:2026-10-31`（区间）
- **模板**：不确定就先调 `list_report_templates`；没有特别要求时用 `monthly_ecom`

### 2. 生成报告包

调用 `build_report(period, template)`：

| 返回 status | 下一步 |
|---|---|
| `succeeded` | 直接进第 3 步 |
| `queued` / `running` | 每 5 秒调用 `get_report(job_id)`，最多 24 次（2 分钟） |
| `failed` | 把 `error.code` + `error.message` 原样转述给用户，**不要自己造数据** |

### 3. 读取内容

调用 `get_report_spec(job_id)` 拿 report.json 原文。
如果返回被截断，或需要插图，再读 `spec_path` 指向的文件和 `artifacts` 里的图片。

### 4. 渲染

PPT 按 `references/slide-template.md` 的页序做；飞书文档用同样的层级（标题 / 表格 / 图片）。
字段含义和取用规则见 `references/report-spec.md`。

### 5. 交付

- 配了企微/飞书 MCP → 发到群，附一句话摘要（GMV、环比、最值得说的一点）
- 没配 → 给出文件路径 + `spec_path`，让用户自己取

## 铁律（违反会直接毁掉报告的可信度）

1. **数字只引用，不重算**：用 `metrics[].value` + `unit`；不要"约 185 万"，不要换算单位。
2. **`narrative` 可以原样用**，但不许改它里面的数字。
3. **`caveats` 必须出现在页面上**（页脚或附录）——口径是报告的一部分，不是可选项。
4. **`skipped` 的节要明说**："本节缺少表 xxx，未生成"，不要用别的数据补。
5. **不要为了"核对"去调 `query_sql`**：报告包就是唯一事实源；要溯源就展示 `meta.queries`。
6. **`schema_version` 主版本不是 `1`** → 停止并说明不兼容；`1.x` 内新增的可选字段（如 `coverage` / `warnings`）直接忽略即可，不要报错。
7. **没有本次的 `report.json` 就不出稿**：工具缺失、任务失败、内容为空，都宁可交白卷并说明原因，绝不拿历史数据或示例凑数。
8. **不用"上一次成功"的报告**：除非用户明确说"就用上次那份"，并且你告诉他那份的生成时间与周期。
9. **`meta.warnings` 必须展示**：至少放封面或 KPI 页页脚；`sections[].coverage.complete == false` 的页要标注"数据区间 min ~ max（days/expected_days 天）"。
10. **涨跌只读 `metrics[].compare`**：`pct` 有值显示 `+8.6%` / `-3.2%`；`pct` 为 `null` 显示 `—` 并把 `reason` 写进页脚。**不许自己用 `value / prev - 1` 重算**。

## 失败处理

| 情况 | 动作 |
|---|---|
| 4 个报告工具缺失 | 按"第 0 步"回复用户并停止，不做任何替代 |
| `status=failed` | 原样转述 `error`；给出 `spec_path` 便于排查；不出 PPT |
| 图表文件不存在 | 用同节的 `tables` 做简表，并注明"图表缺失"；不要画假图 |
| 用户要"把数字改一下" | 拒绝：数字来自 SQL；要改口径请改模板或指标定义 |
| 报告里有 `skipped` 节 | 在对应页或附录写明缺哪张表，不要静默略过 |
| 用户催"先用样例出一版" | 拒绝并解释：示例数据出稿会被当成真实经营结论，风险不可接受 |

## 参考

- `references/report-spec.md`：字段含义与取用规则
- `references/slide-template.md`：PPT 页序、每页取哪些字段

> 本技能**刻意不提供数据样例**：任何看起来像真实数字的样例，都有被直接拿去出稿的风险。
> 需要看真实结构时，用 `get_report_spec` 读本次任务的返回；字段说明看 references。
