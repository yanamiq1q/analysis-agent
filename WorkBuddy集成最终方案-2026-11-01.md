# WorkBuddy 集成最终方案（2026-11-01）

> 依据：`workbuddy 连接器介绍.txt`（MCP 配置、两级 mcp.json、插件体系）
> 前置文档：[多Agent技术入门](多Agent技术入门-2026-11-01.md)、[Agent接入方式对比](Agent接入方式对比-2026-11-01.md)
> 状态：**已确认（2026-11-01）**；PR1 已完成，见文末「实施进度」

---

## 0. 决议摘要

| 决策 | 结论 | 一句话理由 |
|---|---|---|
| 接入方式 | **MCP Server（我们）↔ WorkBuddy（Host）** | 连接器文档写明 WorkBuddy 自己配 `mcp.json`、自己调工具，方向只能是这个 |
| 传输 | **stdio 优先**（`python -m mcp_server`）；URL 形式待 5 分钟验证 | 文档只给了 `command/args/env`；若连接器支持 URL，改用 `/mcp` 更省事 |
| 谁编排 | **WorkBuddy**（它决定什么时候做 PPT/发飞书） | 它是 Host，有 skills / hooks / slash commands |
| 我们提供什么 | **数据 + 口径 + 结构化报告包（ReportSpec）+ 任务状态** | 我们的强项是"数字对"，不是排版 |
| 他们提供什么 | **PPT / 飞书文档 / 群通知** | 它的强项是呈现与分发 |
| LangGraph | **用于"报告流水线"，不用于"连接"** | 连接是 MCP 的事；编排才是图的事 |
| A2A | **不做**，写一份 ADR 说明为什么 | 这是 host→tool，不是 peer→peer |

---

## 1. 事实核对：WorkBuddy 是什么角色

从连接器文档能确定的事实：

| 事实 | 出处 | 推论 |
|---|---|---|
| 在「插件 → MCP 服务器 → 配置 MCP」里填 `mcp.json` | 文档 2）3） | **WorkBuddy 是 MCP Host**，它启动并调用 MCP Server |
| 配置项是 `command` / `args` / `env` | 文档示例 | 支持 **stdio** 传输（本机子进程） |
| 有用户级 `~/.workbuddy/mcp.json` 和项目级 `<项目>/.workbuddy/mcp.json` | 文档 | 我们该用**项目级**，跟项目走，不污染全局 |
| 插件包含：Skills、MCP 连接、Slash Commands、Hooks、Agents | 文档 | WorkBuddy 自己能做"定时/快捷指令/hook"——**编排层不用我们写** |
| 官方连接器 = 预先配好的 MCP | 文档 Q&A | 自定义连接器就是接自己的 MCP Server，正是我们要做的 |
| 状态灯 🟢/🔴 | 文档 4） | 启动失败要能快速看出来 → 我们的 stdio 进程必须"坏得明显" |

**结论：架构方向从"我们当 Host 调它"改成"我们当 Server 被它调"。**
这让方案简单了一大截，也意味着：
- 不需要写 MCP 客户端（异步桥那套）
- 不需要写假的 workbuddy
- PPT/飞书/通知全部留给 WorkBuddy 的插件与技能

---

## 2. 问题一：能不能用 LangGraph？

### 2.1 先分清三层，别把三样东西混成一个

| 层 | 问题 | 用什么 | 不是什么 |
|---|---|---|---|
| **连接层** | 两个系统怎么说话 | **MCP**（stdio / HTTP） | LangGraph 不能替代 MCP |
| **编排层** | 一件事分几步、失败怎么退 | **LangGraph**（图 / 状态 / 断点） | MCP 不能替代编排 |
| **对等层** | 两个 Agent 互相派任务 | **A2A**（任务生命周期 / 回调） | 我们这个场景用不上 |

所以"用 LangGraph 架构"这句话要拆开听：**它能让"报告生成"这件事变成一张可恢复的图**，
但它不会让 WorkBuddy 和 data-agent 连上——连接是 MCP 干的。

### 2.2 在报告流水线里，LangGraph 具体解决什么

报告生成不是一次 LLM 调用，而是一条链：

```
resolve_metrics → plan_sql(每节) → guard(审批策略) → execute → chart → compose_spec → save
```

用图的四个实打实的好处：

| 收益 | 没有图会怎样 | 有图会怎样 |
|---|---|---|
| **断点续跑** | 第 5 节失败，前 4 节的查询全部重跑 | checkpointer 记住已完成节点，从失败处继续 |
| **人工审批** | 要么全自动（危险），要么全人工（不可自动化） | `interrupt()` 停在"需要确认"的节点，`approve_report` 恢复 |
| **阶段进度** | 只有"跑着/跑完" | `stage=querying`、`progress=0.6`，`get_report` 能报 |
| **与现有代码一致** | 另写一套状态机 | 复用 `ReactState` / `guard` / 审批策略的心智模型 |

**这四点里，第 2 点最重要**：你刚做完的 HITL 是一套"计划 → 判定 → 人工确认 → 执行"的机制，
批量报表天然需要同一套东西（模板节自动通过，临时节要人点头）。
用图，这套机制可以直接复用；不用图，就得手写一遍状态机——那才是真的重复。

### 2.3 反过来：哪些地方**不该**用 LangGraph

- ❌ 不要用它包 MCP 连接（LangGraph 不是协议层）。
- ❌ 不要做 supervisor / 多 worker 并行（报告就是一条线性链，拆了只会难调试）。
- ❌ 不要在 WorkBuddy 那一侧要求它用 LangGraph（它是别人家的产品）。

---

## 3. 问题二：我们这样连 WorkBuddy，算不算 A2A？

### 3.1 不算。区别不在"是不是两个 Agent"，在"谁对任务负责"

| 维度 | 我们现在的做法（MCP） | A2A |
|---|---|---|
| 关系 | **Host → Tool** | **Peer → Peer** |
| 被调用方有没有"自己的任务" | 没有：一次调用一件事，返回就结束 | 有：task 有自己的生命周期（submitted/working/input-required/completed） |
| 谁负责整件事 | WorkBuddy 的 LLM | 各自负责各自那段 |
| 能不能反问 | 不能（只能返回错误或"需要更多参数"） | 能（`input-required`，可以要求对方补充信息） |
| 能不能主动推送 | 不能（MCP 没有跨调用的回调） | 能（push notifications） |
| 发现机制 | 手工配 `mcp.json` | Agent Card 自动发现 |
| 跨组织 | 弱（要互相信任同一台机器/网络） | 强（为跨厂商设计） |

**一句话**：MCP 管"手"（能用什么能力），A2A 管"嘴"（谁来派活、活干到哪了）。

我们有 LLM、有工具循环（`ask_data` 内部还是 ReAct），但对外暴露的**契约**仍然是一次请求-响应。
**契约形态决定它是不是 A2A，而不是内部实现。**

### 3.2 什么场景才真的需要 A2A

满足任意两条以上才值得上：

1. **双方都有自主任务**：对方也可能主动给你派活（双向）。
2. **需要协商**：不是"给我数据"，而是"我要 10 月剔除退款口径的 GMV，你能提供吗？不能的话用哪个近似口径？"
3. **长任务 + 需要回调**：对方跑 20 分钟，完成要主动通知你，而不是你去轮询。
4. **跨组织/跨厂商**：你不能假设对方的技术栈，也不能直连它的数据库。
5. **动态发现**：agent 数量会变，靠注册中心按能力找，而不是写死配置。

我们的场景：单向、短契约、同机、无协商、无发现——**一条都不满足**。
所以上 A2A 只会得到一个"更重的 MCP"。

### 3.3 但简历上不能吹成 A2A

正确的说法是：**"designing an agent-as-tool integration over MCP, with job semantics for long-running work"**。
面试官听到 "A2A" 会追问 Agent Card、task lifecycle、push——答不上来反而扣分。

---

## 4. 最终架构

```
┌──────────────────────────────────────────────────────────────┐
│ WorkBuddy（Host / 编排层）                                     │
│  LLM + Skills + Slash Commands + Hooks + 插件                  │
│  例：/月报  → 调 data-agent → 读 report.json → 生成 PPT/飞书   │
│       → 通过企微/飞书 MCP 发群                                │
└───────────────────────┬──────────────────────────────────────┘
                        │ MCP（stdio：python -m mcp_server）
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ data-agent（MCP Server / 数据层）                              │
│  工具面：build_report / get_report / approve_report            │
│          （+ 现有 list_tables / query_sql / ask_data / …）     │
│  编排：ReportGraph（LangGraph，线性 6 节点）                    │
│  事实：ReportSpec + 图表 + 口径                                │
└───────────────────────┬──────────────────────────────────────┘
                        ▼
        data/warehouse/report_jobs/{job_id}/
          report.json     ← 唯一事实源（数字不可被渲染方修改）
          assets/*.png    ← 图表文件
          status.json     ← 阶段/进度/错误（文件持久化，重启可查）
          deck.pptx       ← WorkBuddy 写入（可选）
```

**职责边界一句话**：data-agent 对"数字对不对"负责，WorkBuddy 对"好不好看、发没发出去"负责。

---

## 5. 工具面设计（新增 3 个 MCP 工具）

| 工具 | 入参 | 返回 | 副作用 | annotations |
|---|---|---|---|---|
| `list_report_templates` | — | 模板名 + 说明（如 `monthly_ecom`） | 无 | readOnly |
| `build_report` | `period`（`2026-10` 或 `start/end`）、`template`、`focus?`、`wait_seconds?=20` | 见下 | 写 job 目录 | 非只读、local write |
| `get_report` | `job_id` | `status/stage/progress/spec_path/artifacts/needs_approval` | 无 | readOnly |
| `approve_report`（v1.5） | `job_id`、`decision`、`note?` | 恢复后的状态 | 继续执行 | 非只读 |

`build_report` 的返回设计（关键）：

```jsonc
// 已完成（大多数情况，wait_seconds 内跑完）
{ "job_id": "rpt-2026-10-monthly_ecom-a1b2", "status": "succeeded",
  "spec_path": "data/warehouse/report_jobs/rpt-.../report.json",
  "artifacts": [".../assets/gmv_by_channel.png"], "summary": "4 节 / 12 个指标 / 3 张图" }

// 还在跑（超过 wait_seconds）
{ "job_id": "...", "status": "running", "stage": "charts", "progress": 0.6,
  "hint": "请每 5 秒调用 get_report(job_id) 直到 status 不再是 running" }

// 需要人确认（v1.5）
{ "job_id": "...", "status": "needs_approval",
  "reason": "第 2 节预估扫描 120 万行",
  "hint": "向用户确认后调用 approve_report(job_id, decision)" }
```

工具描述里必须写清"**轮询直到非 running**"——Host 的 LLM 是看描述决定行为的。

---

## 6. 数据契约：ReportSpec v1

```jsonc
{
  "schema_version": "1.0",
  "job_id": "rpt-2026-10-monthly_ecom-a1b2",
  "template": "monthly_ecom",
  "title": "2026 年 10 月电商经营分析",
  "period": { "start": "2026-10-01", "end": "2026-10-31" },
  "sections": [
    {
      "id": "gmv",
      "heading": "GMV 概览",
      "narrative": "10 月 GMV 1,234 万元，环比 +12.3%……",
      "metrics": [
        { "name": "gmv", "label": "GMV", "value": 12340000, "unit": "元",
          "qoq": 0.123, "yoy": null, "definition": "下单金额（含未支付）" }
      ],
      "tables": [ { "columns": ["渠道", "GMV"], "rows": [["抖音", 8000000]],
                    "source_sql": "SELECT ..." } ],
      "charts": [ { "kind": "bar", "title": "渠道 GMV 对比",
                    "image_path": "assets/gmv_by_channel.png",
                    "data_path": "assets/gmv_by_channel.json" } ],
      "caveats": ["口径：下单即计入，未扣退款"]
    }
  ],
  "meta": {
    "generated_at": "2026-11-01T09:00:00+08:00",
    "tables_used": ["ods_order_info"],
    "queries": ["SELECT ..."],
    "metric_version": "metrics@2026-10",
    "trace_id": "..."
  }
}
```

四条铁律（和 HITL 的"确认的 SQL == 执行的 SQL"同源）：

1. 数字是 `number`，叙述与数字分离，渲染方**只排版不改值**；
2. 图表落文件（PNG + 数据 JSON），不塞 base64；
3. 口径随包走（`definition` / `caveats` / `metric_version`）；
4. 失败是 `status=failed` + `error.code`，不是一段会被排进 PPT 的文字。

---

## 7. 任务模型

| 项 | 设计 |
|---|---|
| 状态机 | `queued → planning → querying → charting → composing → succeeded`；异常 `failed`；v1.5 加 `needs_approval` |
| 幂等 | `job_id = rpt-{period}-{template}-{hash(focus)[:4]}`；重复提交返回同一个 job |
| 超时 | 单节 SQL 用现有 `QUERY_TIMEOUT`；整单上限 `REPORT_TIMEOUT`（默认 10 分钟） |
| 持久化 | `status.json` 落盘：重启后 `get_report` 仍能查到（jobs 字典丢了也能从盘上恢复） |
| 并发 | 同一 job 只允许一个执行线程；不同 job 可并行（DuckDB 连接锁串行） |
| 清理 | 超过 7 天的 job 目录可删（配置项） |

---

## 8. 必须先解决的运营问题：DuckDB 是单进程独占

`python -m mcp_server` 是**独立进程**，它会打开 `data/warehouse/analysis.duckdb`。
而 web 服务也开同一个文件——**P0-1 已经验证：第二个进程连只读都打不开**。

所以有三种走法，按优先级：

| 方案 | 做法 | 代价 |
|---|---|---|
| **A. WorkBuddy 用 URL 连 `/mcp`** | 连接器里试 `{"url": "http://127.0.0.1:8000/mcp"}` | 若支持，最干净：单进程、web 和 WorkBuddy 共存 |
| **B. 只用 stdio，不开 web** | 上传用 `load_file`（文件放 `data/warehouse/`），MySQL 用新增的 `connect_mysql` 工具 | 不能用网页看图表；需要多暴露一个工具 |
| **C. stdio 做代理** | stdio 进程不碰 DuckDB，转调 web 的 HTTP API | 多一层代码；适合 v2 |

**行动**：先用 5 分钟验证 A（看连接器支不支持 URL 类型）。不支持就用 B，并把
`mcp_server` 启动时的"数据库被占用"错误改成**直接退出并打印人话**，让状态灯变红（而不是第一个工具调用才报错）。

---

## 9. 实施计划（4 个 PR，每个都可独立验收）

### PR1：报告契约 + 流水线骨架（2~3 天）

- `data/report/spec.py`：`ReportSpec` 数据类 + 校验 + 序列化
- `data/report/pipeline.py`：`ReportGraph`（LangGraph 线性 6 节点，先不接 HITL）
- `data/report/templates/monthly_ecom.yaml`：模板（节 + 指标 + 图表类型）
- `data/warehouse/report_jobs/` 目录 + `status.json` 读写

**验收**：命令行跑 `python -m data.report.run --period 2026-10 --template monthly_ecom`，
产出 `report.json + assets/*.png`，数字能对上 SQL。

### PR2：MCP 工具面（1~2 天）

- `mcp_server/server.py` 增加 `list_report_templates` / `build_report` / `get_report`
- 后台线程执行 + `status.json` 持久化 + 幂等 job_id
- 工具描述按"Host 的 LLM 会怎么读"来写（含轮询提示）

**验收**：WorkBuddy 里配好 `mcp.json` → 说"生成 10 月月报" → 状态灯绿 → 拿到 `report.json`。

### PR3：端到端（PPT / 飞书）（2~3 天，主要工作在 WorkBuddy 侧）

- WorkBuddy 建一个插件/技能：读 `report.json` → 按模板生成 PPT/飞书文档
- 用 WorkBuddy 的企微/飞书 MCP 发群
- 我们这侧只需保证 `report.json` 字段稳定 + 图表路径可读

**验收**：一句"生成本月经营分析并发到群里" → 群里收到 PPT/文档链接。

### PR4：审批与可观测（2 天，可选但加分）

- `approve_report` + 审批策略（模板节自动、临时节人工）
- `trace_id` 贯穿：`build_report` 生成，写进 `report.json.meta` 与日志
- 失败路径测试：超时、重复提交、产物缺失、WorkBuddy 中途重启

**验收**：高风险节触发 `needs_approval` → 确认后恢复；`get_report` 在进程重启后仍能查到。

---

## 10. 风险与备选

| 风险 | 概率 | 备选 |
|---|---|---|
| WorkBuddy 不支持 URL MCP | 高 | 方案 B（stdio-only），多暴露 `connect_mysql` |
| WorkBuddy 工具调用超时 < 60s | 中 | `build_report` 默认 20s 返回 job_id，强制轮询 |
| WorkBuddy 的 PPT 技能不认 `report.json` | 中 | 加一个 `report.md`（人读版）+ `report.plain.txt`，让它先用文本 |
| 报告里数字被 WorkBuddy 改 | 中 | 提供 `metrics` 数组 + 在文档里标注"数字只许引用不许重算" |
| DuckDB 锁冲突 | 高 | 启动即失败 + 文档写清"二选一" |

---

## 11. 演示脚本（面试用，3 分钟）

1. WorkBuddy 里说：「生成本月电商经营分析，重点看渠道」
2. 观察：WorkBuddy 调 `build_report` → 我们的图跑 4 个节点（prepare → execute → charts → compose） → `get_report` 轮询 → 返回 `report.json`
3. WorkBuddy 生成 PPT + 发到群
4. 打开 `report.json`，指着某个数字说：**"这个数来自这条 SQL，口径是这条定义"**
5. 切到高风险场景：某节扫描 120 万行 → `needs_approval` → 人在 WorkBuddy 里点确认 → 恢复
6. 收尾一句：**"MCP 负责连接，LangGraph 负责编排，A2A 我们评估过但不需要"**

---

## 12. 需要你确认的 3 件事（都很快）

1. 连接器里**能不能填 URL 类型**的 MCP server？（决定方案 A 还是 B）
2. WorkBuddy 生成 PPT / 飞书文档，是**它自带技能**还是要我们再给它一个 MCP？
3. `build_report` 的默认等待时间给 **20 秒**可以吗？（超过就返回 job_id 轮询）

---

## 13. 实施进度（2026-11-01 更新）

### PR1 已完成：报告契约 + 流水线骨架

新增代码：

```
data/report/__init__.py
data/report/spec.py        ReportSpec 契约（数字类型校验 / JSON 安全 / 自检）
data/report/charts.py      图表渲染（与 plot_chart 共用一份实现，串行化渲染）
data/report/pipeline.py    LangGraph 4 节点：prepare → execute → charts → compose
data/report/run.py         CLI（--list-templates / --period / --force）
data/report/templates/monthly_ecom.yaml
tests/test_report.py       23 条
```

真实仓库跑通（python -m data.report.run --template monthly_ecom --period 2026-10）：

```
报告: data/warehouse/report_jobs/rpt-2026-10-01_2026-11-01-monthly_ecom/report.json
4 节 / 3 个指标 / 2 张图
  - 核心指标: 3 指标 / 0 图 / 0 表
  - 每日 GMV 趋势: 0 指标 / 1 图 / 0 表
  - 订单状态结构: 0 指标 / 1 图 / 1 表
  - 高价值用户 Top 5: 0 指标 / 0 图 / 1 表
```

报告内容示例：GMV 1,846,900 元，订单 1,357 单，平均支付率 71.1%；
每节都带 queries 与 caveats（例如"ods_order_info 没有日期列，全量口径"）。

### PR1 里做的四个决定

1. **报表不调 LLM**：SQL 来自模板，确定性优先。LLM 只服务临时问答（sk_data）。
   好处：不需要 key、可回归测试、同一个周期每次跑出同样的数。
2. **单行聚合自动成指标**：模板没写 metrics 且结果只有一行时，每一列自动变成指标；
   否则 SELECT count(*) 这种节会产出空 section。
3. **缺表不报错，只标 skipped**：报告照样出，人能看到"哪一节缺数据"，
   比整份失败更有用。
4. **JSON 安全**：日期列（Timestamp）与 NaN 在落盘前统一转换，否则对方严格解析器会炸。

### 待办

- PR2：MCP 工具（uild_report / get_report / list_report_templates），
  先把方案 A（URL）验证掉，不行再走 stdio-only。
- PR3：WorkBuddy 侧 PPT/飞书模板（自带技能）。
- PR4：pprove_report（复用 interrupt）+ trace。
