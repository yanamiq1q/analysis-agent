# 报告流水线 PR1 说明：从模板到 ReportSpec

> 时间：2026-11-01
> 目标：让 data-agent 产出一份**结构化、可校验、数字不可篡改**的报告包，供 WorkBuddy 渲染成 PPT / 飞书文档。
> 状态：已完成，`302 passed`（其中 23 条属于本模块）
> 关联：[WorkBuddy 集成最终方案](WorkBuddy集成最终方案-2026-11-01.md)

---

## 1. 为什么需要这一层（先说结论）

聊天接口返回的是一段**散文**。把散文交给另一个 Agent 做 PPT，会发生三件事：

1. 它得再用 LLM 解析一遍，**数字会被"润色"**（1,846,900 变成"约 185 万"）；
2. 口径丢了——报告里写"GMV 184 万"，但没人知道这是"下单金额"还是"支付金额"；
3. 分析失败时，"查询超时"这句话会被排版成一张**正经的 PPT**。

所以跨系统只传结构化数据，而且这份结构要满足四条铁律：

| 铁律 | 具体做法 |
|---|---|
| 数字是 `number` | 值字段只允许 int/float，字符串数字直接判错 |
| 叙述与数字分离 | `narrative` 里可以有"约"，`metrics[].value` 必须精确 |
| 图表是文件 | `assets/*.png` + 同名 `.json`（原始数据），不塞 base64 |
| 口径随包走 | `definition` / `caveats` / `meta.queries` |

---

## 2. 架构：四个节点

```
START → prepare → execute → charts → compose → END
          │          │         │         │
          │          │         │         └─ 组装 ReportSpec + 自检 + 落盘
          │          │         └─ 渲染 PNG / 数据 JSON
          │          └─ 逐节执行 SQL（单节失败只标 skipped）
          └─ 读模板、替换周期、检查表是否存在
```

为什么用 LangGraph 而不是一个大函数：

- v1 里它确实是线性的，**图的收益在"后面要加的东西"**：
  - 某节失败从断点重跑（checkpointer）；
  - 某节风险过高时停下等人确认（interrupt，复用 HITL）；
  - 阶段进度（`stage`）天然对应节点。
- 现在写成大函数，这三件事以后都要手工补状态机。

**v1 刻意不做的事**：

| 不做 | 原因 |
|---|---|
| 不调 LLM | 报表 SQL 来自模板，确定性优先；LLM 只服务临时问答 |
| 不并行 | DuckDB 单连接，并行只会排队，还让进度变乱 |
| 不自动重试 | 失败要让人看见，静默重试会把问题藏起来 |

---

## 3. 文件与职责

| 文件 | 职责 | 关键点 |
|---|---|---|
| `data/report/spec.py` | ReportSpec 契约 | 数字类型校验、JSON 安全、`validate()` 自检（[spec.py:140](data/report/spec.py#L140)） |
| `data/report/charts.py` | 图表渲染 | 与 `plot_chart` **共用一份实现**；matplotlib 全局状态加锁（[charts.py:43](data/report/charts.py#L43)） |
| `data/report/pipeline.py` | 四节点流水线 | 模板解析、周期、缺表降级、组装（[pipeline.py:146](data/report/pipeline.py#L146)） |
| `data/report/templates/monthly_ecom.yaml` | 示例模板 | 4 节：指标 / 趋势 / 结构 / TopN |
| `data/report/run.py` | CLI | `--list-templates` / `--period` / `--force`（[run.py:28](data/report/run.py#L28)） |
| `tests/test_report.py` | 23 条测试 | 契约、模板、端到端、幂等、JSON 安全、图表 |

---

## 4. 契约字段（给对方渲染方看的）

```jsonc
{
  "schema_version": "1.0",
  "job_id": "rpt-2026-10-01_2026-11-01-monthly_ecom",
  "template": "monthly_ecom",
  "title": "2026 年 10 月电商经营分析",
  "period": { "start": "2026-10-01", "end": "2026-11-01" },
  "sections": [{
    "id": "kpi",
    "heading": "核心指标",
    "narrative": "本期 GMV 1,846,900 元，订单 1,357 单，平均支付率 71.1%。",
    "metrics": [{
      "name": "gmv", "label": "GMV", "value": 1846900,
      "unit": "元", "format": "money",
      "definition": "ads_order_daily_report.gmv 在区间内求和"
    }],
    "tables": [{ "columns": [...], "rows": [...], "source_sql": "SELECT ..." }],
    "charts": [{
      "kind": "line", "title": "每日 GMV",
      "image_path": "assets/gmv_trend.png",
      "data_path": "assets/gmv_trend.json"
    }],
    "caveats": ["数据来自日汇总表 ads_order_daily_report"],
    "skipped": ""                        // 非空 = 这一节没生成，原因写在这
  }],
  "meta": {
    "generated_at": "...", "template": "monthly_ecom",
    "period": {...}, "tables_used": [...], "queries": [...],
    "skipped_sections": 0, "trace_id": "trace-...", "engine": "data-agent report pipeline"
  }
}
```

`validate()` 会挡住三类常见错误：数字被写成字符串、图表路径不存在、`generated_at` 缺失。

---

## 5. 四个设计决定（面试可以直接讲）

### 5.1 报表不调 LLM

模板里的 SQL 是**确定性的**。不调 LLM 带来三个好处：不需要 API key、可回归测试、同一周期每次跑出同样的数。
自然语言问答仍然走 `ask_data`（LLM + ReAct），两者互不干扰。

### 5.2 单行聚合自动成指标

模板没写 `metrics` 且结果只有一行时，每一列自动变成指标。
否则 `SELECT count(*) AS n` 这种节会产出空 section，模板作者还得为每个聚合列重复写一遍 name/label。

### 5.3 缺表不报错，只标 `skipped`

报告照样出，人能看到"哪一节缺数据"。整份失败是最差的体验——用户不知道是没数据还是代码坏了。

### 5.4 图表只有一份实现

`plot_chart` 原来自己画图，报告也需要画图。如果各写一套，中文字体、数值列判断、饼图规则会慢慢分叉（同一个数据在聊天里和报告里画出来不一样）。
现在统一到 [charts.py](data/report/charts.py)，并在渲染外面加锁——matplotlib 全局状态不是线程安全的，报告 job 在自己的线程里跑。

---

## 6. 踩过的坑（都是真实报错）

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | `Object of type Timestamp is not JSON serializable` | 日期列是 `pd.Timestamp`，跑完落盘时才炸 | `chart_data` 逐单元格转 ISO 字符串；`spec.write` 再加一层 `default=` |
| 2 | `NaN` 被写进 report.json | `where(notna, None)` 在 float 列上仍是 NaN（dtype 没变 object） | 先 `astype(object)` 再填 None，得到真正的 `null` |
| 3 | 只有 SQL 没有 `metrics` 的节产出空 section | 指标只从模板的 `metrics` 配置来 | 单行结果自动把每列变成指标 |
| 4 | 测试断言 `== 4` 失败 | 单行自动成指标这个默认行为改变了返回结构 | 测试改成按列名断言（`n == 4`） |
| 5 | 中文标题变成方块 | 字体只在 `tools.py` 里设置过 | 字体设置随渲染逻辑一起搬到 `charts.py`，并留 DejaVu 兜底 |

---

## 7. 怎么用

```powershell
# 列出模板
python -m data.report.run --list-templates

# 生成 2026 年 10 月报告
python -m data.report.run --template monthly_ecom --period 2026-10

# 指定区间 / 强制重跑
python -m data.report.run --period 2026-10-01:2026-10-31 --force
```

产物：

```
data/warehouse/report_jobs/rpt-2026-10-01_2026-11-01-monthly_ecom/
  report.json       ← 唯一事实源
  assets/
    gmv_trend.png   gmv_trend.json
    order_status.png order_status.json
  status.json       ← status/stage/progress/error
```

真实仓库跑出来的结果：

```
4 节 / 3 个指标 / 2 张图
  核心指标：GMV 1,846,900 元，订单 1,357 单，平均支付率 71.1%
  每日 GMV 趋势：line 图
  订单状态结构：bar 图 + 表（caveat：该表无日期列，全量口径）
  高价值用户 Top 5：表
```

---

## 8. 测试覆盖（23 条）

| 分组 | 覆盖点 |
|---|---|
| 周期 | `2026-10` 半开区间、显式区间含末日、五种非法输入（含注入串） |
| 模板 | 内置模板可加载、未知模板报"可用列表" |
| 端到端 | 4 节全产出、指标数字与 SQL 一致、叙述渲染、图表文件存在、status=succeeded |
| JSON | 无 `NaN`/`Infinity`、日期可序列化、严格 `json.loads` 通过 |
| 幂等 | 第二次提交命中缓存，status.json 不变 |
| 降级 | 缺表 → 该节 skipped、其余正常、报告仍 succeeded |
| 契约 | 字符串数字/缺 generated_at/缺图表文件 → validate 报错 |
| 图表 | PNG magic bytes、单列报错、未知图型报错、NaN→null |

---

## 9. 局限与下一步

| 局限 | 影响 | 下一步 |
|---|---|---|
| 只有 CLI，宿主还调不到 | WorkBuddy 无法触发 | **PR2：MCP 工具 `build_report` / `get_report`** |
| 模板 SQL 靠手写 | 新增指标要改 YAML | 让模板可引用指标注册表（`metric: gmv`） |
| 无审批 | 高风险 SQL 直接执行 | PR4：`approve_report` 复用 interrupt |
| 无跨重启恢复 | 进程重启后 running 的 job 变孤儿 | PR2 用 status.json 兜底；PR4 接 checkpointer |
