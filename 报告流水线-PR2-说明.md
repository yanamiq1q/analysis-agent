# 报告流水线 PR2 说明：把报告变成宿主可调用的 MCP 工具

> 时间：2026-11-01
> 目标：WorkBuddy 里说一句"出本月经营报告"，它就能调到 data-agent 的报告能力。
> 状态：已完成，`312 passed`（PR1 的 23 + PR2 的 10 条在上次 302 的基础上）
> 前置：[PR1 说明](报告流水线-PR1-说明.md)、[WorkBuddy 集成最终方案](WorkBuddy集成最终方案-2026-11-01.md)

---

## 1. 为什么不是"一个同步工具"

MCP 的工具调用是**一次请求-响应**：宿主发 `tools/call`，等结果，超时就算了。
而一份报告要跑十几条 SQL、画几张图，通常 2~10 秒，数据多的时候更久。

同步实现会踩两个坑：

1. **宿主超时**：WorkBuddy 等不到就报"工具调用失败"，但报告其实还在跑——
   用户重试一次，就有两个 job 在写同一个目录；
2. **没有进度**：跑 8 秒期间界面完全空白，用户不知道是在跑还是卡了。

所以采用 MCP 里事实标准的「提交 + 轮询」：

```
build_report(period, wait_seconds=20)
   ├─ 20 秒内跑完 → 直接返回 succeeded + spec_path + artifacts
   └─ 还没跑完    → 返回 running/queued + job_id + hint("每 5 秒调 get_report")
```

`wait_seconds` 的设计是"照顾大多数"：常见报告 2~3 秒就完成，一次调用拿全；
只有大报告才需要轮询。

---

## 2. 新增的三个工具

| 工具 | 入参 | 返回 | 注解 |
|---|---|---|---|
| `list_report_templates` | — | `[{name, title, description, sections}]` | readOnly |
| `build_report` | `period`、`template`、`focus`、`wait_seconds`、`force` | `{status, stage, spec_path, artifacts, summary, hint}` | local write |
| `get_report` | `job_id` | 同上（实时状态） | readOnly |

工具描述（docstring）是给宿主的 LLM 看的，所以里面明确写了三件事：

- "本工具**不生成 PPT**，只产出报告包，PPT/飞书由你（宿主）渲染"；
- "status 是 running 时，**每 5 秒**调用 `get_report`，不要重复调用 `build_report`"；
- "同一周期会命中同一个 job，不会重跑"。

代码位置：[mcp_server/server.py:365](mcp_server/server.py#L365)（模板）、
[:386](mcp_server/server.py#L386)（提交）、[:430](mcp_server/server.py#L430)（查询）。

---

## 3. 任务层设计

### 3.1 状态机

```
queued → planning → querying → charting → composing → succeeded
                                                      ↘ failed
```

- `queued`：提交瞬间就写，宿主立刻能查到"已收到"；
- 中间四个 stage 由流水线各节点写（复用 PR1 的 `write_status`）；
- `succeeded` / `failed` 是终态。

### 3.2 状态存在文件里，不在内存里

`data/warehouse/report_jobs/{job_id}/status.json` 是唯一真相。
内存里的 `_jobs` 只用来回答"这个 job 是不是我提交的"。

带来的两个好处：

- **进程重启后 `get_report` 仍然能回答**（测试 `test_status_survives_manager_reset`）；
- 如果进程在 `running` 时被杀，下次查询会看到 `stale: true` + "请用 force 重新提交"
  （靠 `started_at` 与 `REPORT_TIMEOUT_SECONDS` 判断）。

### 3.3 幂等

`job_id = rpt-{start}_{end}-{template}[-hash(focus)]`，由请求内容决定：

- 重复提交同一周期 → 同一个 job，不会跑两遍；
- 已 `succeeded` 的任务 → 直接读回缓存，连 `run_report` 的图都不会重画。

### 3.4 单 job 单线程

`submit` 在锁里检查 `_jobs`，已有记录就不再提交线程；线程池上限 2，
不同 job 可以并行（但 DuckDB 连接锁会把真实执行串行化——这没关系，报告不是高频操作）。

---

## 4. 真实交互长什么样

WorkBuddy 调 `build_report`（20 秒内完成时）：

```json
{
  "job_id": "rpt-2026-10-01_2026-11-01-monthly_ecom",
  "status": "succeeded",
  "stage": "done",
  "progress": 1.0,
  "spec_path": "C:\\...\\report_jobs\\rpt-.../report.json",
  "artifacts": [
    "C:\\...\\report.json",
    "C:\\...\\assets\\gmv_trend.png",
    "C:\\...\\assets\\gmv_trend.json",
    "C:\\...\\assets\\order_status.png",
    "C:\\...\\assets\\order_status.json"
  ],
  "summary": "4 节 / 3 个指标 / 2 张图"
}
```

没跑完时：

```json
{
  "job_id": "...",
  "status": "running",
  "stage": "charting",
  "progress": 0.6,
  "hint": "任务仍在进行：请每 5 秒调用 get_report(job_id) 直到 status 变为 succeeded/failed"
}
```

宿主拿到 `succeeded` 后的动作（这是它的事，不是我们的）：
读 `report.json` → 按模板生成 PPT / 飞书文档 → 用它的 MCP 发到群。

---

## 5. 踩坑记录

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | `wait_seconds=0` 时返回 `status: unknown` | 目录还没被工作线程创建，`payload()` 找不到 status.json | `submit()` 里先建目录并写 `queued`，再提交线程 |
| 2 | 模板名写错，工具返回成功 + 后台 failed | 模板校验在 `run_report` 里（后台线程） | 工具里先同步 `load_template()`，错模板立刻抛错 |
| 3 | `queued` 状态没有 `hint` | 只在 `running` 分支加了提示 | `queued/running` 都加；`running` 才判 stale |
| 4 | 提交已成功的 job 会把状态刷成 `queued` | 重启后内存丢了，重建时无脑写 queued | 读旧 status：已 succeeded 且非 force 就不覆盖 |
| 5 | 第二个 uvicorn 起不来 / 报告报"数据库被占用" | DuckDB 单进程独占（P0-1） | 这是**预期行为**：一个进程同时提供聊天和报告；不要开第二个 |

第 5 条值得强调：**WorkBuddy 连的那个 8000 端口的进程，就是唯一的数据所有者**。
报告 job 在它内部跑，不会再开 DuckDB。所以不存在"聊天和报告抢锁"。

---

## 6. 测试（10 条）

| 关注点 | 测试 |
|---|---|
| 模板列表 | `test_lists_builtin_template`（含 section 名） |
| 同步完成路径 | `test_sync_path_returns_finished_report`（产物存在、数字与 SQL 一致） |
| 轮询路径 | `test_polling_path_when_not_finished_in_time` |
| 幂等 | `test_same_period_is_idempotent`（job_id 相同、finished_at 不变） |
| 单节失败降级 | `test_failed_report_reports_error_not_crash`（全部 skipped 但 succeeded） |
| 非法周期 | `test_bad_period_rejected` |
| 未知模板 | `test_unknown_template_rejected` |
| 未知 job | `test_unknown_job_rejected` / 空 job_id |
| 重启后仍可查 | `test_status_survives_manager_reset` |

---

## 7. 怎么验证（重要：要重启服务）

你现在 8000 端口跑的是**加 PR2 之前的进程**（只有 8 个工具）。代码已经改了，
必须重启才会加载新工具：

```powershell
# 1) 关掉当前那个 start-server 窗口（Ctrl+C）
# 2) 重新启动
start-server.bat
```

重启后 WorkBuddy 的 MCP 面板应该从 **8/8** 变成 **11/11**，多出：

```
list_report_templates   build_report   get_report
```

然后直接在 WorkBuddy 里说：

> 用 monthly_ecom 模板出 2026 年 10 月的经营报告

它应该先调 `list_report_templates`，再调 `build_report`，返回 `spec_path`。

我在另一个端口用同一份代码验证过（避开你正在跑的 8000）：

```
tools: 11
  list_tables, query_sql, describe_table, preview_table, get_metric_definition,
  load_file, save_metric_definition, ask_data,
  list_report_templates, build_report, get_report
```

---

## 8. 下一步

| 项 | 说明 |
|---|---|
| **PR3（宿主侧）** | 在 WorkBuddy 里建一个技能/快捷指令：读 `report.json` → 生成 PPT / 飞书文档 → 发群 |
| **PR4（可选）** | `approve_report`：高风险 SQL 触发 `needs_approval`，复用 HITL 的 interrupt |
| 小优化 | `report.md`（人读版）——有些 PPT 技能更擅长读 markdown 而不是 JSON |

PR3 主要工作在 WorkBuddy 侧（它的技能/插件），我们这边只需要保证
`report.json` 字段稳定 + 图片路径可读。如果你把 WorkBuddy 的技能配置方式发我，
我可以把"读报告包 → 出 PPT"的提示词和字段映射一起写出来。
