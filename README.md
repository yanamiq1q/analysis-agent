# data-agent

个人数据分析 Agent：自然语言提问 → 自动查数据 → 生成答案（含图表）。
基于 **LangGraph + DuckDB**，参照开源项目 Data-Analysis-Agent 精读后自研，
重点补齐了生产级防线（SQL 安全校验、查询护栏、结构化日志、断线重连）。

## 核心能力

- **统一查询引擎**：CSV / Excel / JSON / Parquet / MySQL 全部接入 DuckDB，一个引擎跑所有 SQL
- **ReAct 多工具循环**：LLM 自主决策——先查表清单，再分组统计，需要时自动画图，最后综合成答案
- **工具调用可视化**：每步工具调用（工具名 / 参数 / 结果摘要）渲染成步骤卡片推给前端，
  文字逐字流出——用户能看见 Agent 在做什么，而不是干等
- **高危 SQL 人工确认（HITL）**：生成的 SQL 先 dry-run（不执行），按四条规则判定风险
  （大表扫描 / 无边界 `SELECT *` / 跨远端库 / 口径偏离）；命中则暂停等你确认，
  **确认的那条 SQL 就是执行的 SQL**（不会重新生成）；拒绝时把你的理由回喂给 Agent 改写
- **指标口径字典**：内置 GMV、订单量等标准口径，命中即**锁定责任表**，禁止换表或用汇总表兜底
- **技能包机制（skill）**：指标口径是 `skills/metrics` 下的 markdown 文件（改口径零代码、不重新发版）；
  系统提示只带一行技能目录（渐进式披露省 token），`load_skill` 激活后注入全文并**按技能
  allowedTools 收窄工具集**（最小权限，只收窄不扩权）
- **MySQL 冷热分层**：小表搬入本地、大表 SQL 整条下推远端执行（sqlglot 方言转译 + 行数采样判定）
- **MCP Server（M9）**：12 个工具（9 只读 + 3 写）暴露给任意宿主；既可在 web 服务里用
  **`/mcp`（推荐，同一进程共享同一份数据）**，也可 `python -m mcp_server` 走 stdio，
  纯套壳复用现有校验与执行链路
- **安全四道防线**：sqlglot AST 白名单校验 → DuckDB 连接级封锁 → 行数/超时护栏（超时用
  `conn.interrupt()` 真打断）→ LLM 超时+重试闭环
- **多轮对话**：会话记忆（每会话串行锁）+ SSE 流式输出 + 断线自动重连
- **无 key 也能起服务**：依赖懒加载，只有自然语言分析需要 `DEEPSEEK_API_KEY`，
  上传/看表/静态页/纯 SQL 的 MCP 工具都不受影响
- **结构化报告包（ReportSpec）**：模板 + SQL → 一份**数字不可篡改**的报告包
  （metrics / tables / charts / 口径 caveats / 每条 SQL），供 WorkBuddy 等宿主渲染成
  PPT / 飞书文档；`python -m data.report.run --period 2026-10` 即可产出

## 架构

```mermaid
flowchart LR
    FE[前端 index.html] -->|SSE 流| API[FastAPI api/server.py]
    API --> REACT[ReAct 外层循环 agent/react.py]
    REACT -->|list_tables / query_sql / plot_chart / load_skill| TOOLS[工具注册表 agent/tools.py]
    TOOLS --> GRAPH[四节点链路 agent/graph.py]
    SKILL[skills/metrics 技能包] --> LOADER[agent/skill_loader.py]
    LOADER -->|口径 registry| GRAPH
    LOADER -->|目录/全文/收窄| REACT
    GRAPH --> VAL[安全校验 data/validate.py]
    GRAPH --> ENG[DuckDB 引擎 data/engine.py]
    ENG ---|冷表下推| MYSQL[MySQL 远端]
    ENG ---|热表搬运| DUCK[(analysis.duckdb)]
```

两层 Agent 分工：

| 层 | 文件 | 职责 |
|---|---|---|
| 外层 ReAct | `agent/react.py` | 看对话上下文决定调哪个工具 / 何时收尾；HITL 时在"决策"和"执行"之间插入 plan/guard |
| 内层 SQL 链 | `agent/graph.py` | collect_schema → generate_sql → validate_sql → execute，含校验/护栏/重试；`plan_only` 模式只生成不执行 |

SSE 事件（前端逐条消费）：

| 事件 | 内容 |
|---|---|
| `node` | 节点完成；`react_agent` 带真实决策文案（如「决定调用 query_sql」），其余为空串 |
| `tool_call` | 工具名 + 参数原文 + 结果摘要（前端渲染成步骤卡片） |
| `approval_required` | HITL：待确认的 SQL + 拦截原因 + `thread_id`（前端渲染成审批卡片） |
| `chart` | base64 PNG 图表 |
| `answer` | 最终答案 |
| `history` | 追加本轮问答后的完整会话历史 |

## 目录结构

```
data-agent/
├── agent/            # Agent 层：graph(四节点) / react(ReAct) / tools(工具集) / llm / skill_loader(技能加载)
├── data/             # 数据层：engine(DuckDB) / service(路由) / validate(sqlglot) / metrics(指标匹配逻辑)
│   ├── guards.py     # 查询执行护栏：daemon 线程 + conn.interrupt() 可中断超时
│   ├── risk.py       # HITL 风险判定：四条纯规则，不调 LLM
│   └── sources/      # 数据源：loaders(文件) / mysql_source(MySQL 冷热分层)
├── skills/           # 技能包：metrics/SKILL.md + references/metrics.md（指标口径，markdown 配置化）
├── app_context.py    # 运行时装配：懒加载 service/LLM + 唯一一份指标注册表（Web/MCP 共用）
├── api/
│   ├── server.py     # FastAPI：chat(SSE) / upload / tables / connect_mysql / mcp 挂载
│   └── sessions.py   # 会话历史 + 每会话串行锁
├── mcp_server/       # MCP Server（M9/PR2）：12 个工具，可 stdio 也可挂到 /mcp
├── data/report/      # 报告流水线（PR1）：spec 契约 / charts / 模板 / LangGraph 管线 / CLI
├── workbuddy-skill/  # WorkBuddy 技能（PR3）：report-producer，读报告包出 PPT/飞书文档
├── frontend/         # 原生 HTML 前端（fetch+getReader 手写 SSE 消费 + 打字机渲染）
├── scripts/          # stdw_init.sql：演示库 DDL（含 1200 行数据生成）
├── tests/            # 331 个测试，全 fake 注入，无需真实 key/MySQL
└── config.py         # 单一配置源：全部阈值集中于此
```

## 快速开始

```powershell
# 1. 安装依赖（Python 3.11+）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 启动服务（不配 key 也能起：上传/看表可用，自然语言分析会提示缺 key）
.venv\Scripts\python -m uvicorn api.server:app --port 8000

# 3. 要问数据时再配 key（懒加载：不用重启也能验证其它能力）
$env:DEEPSEEK_API_KEY = "sk-xxx"          # DeepSeek API key（openai 兼容）

# 4. 浏览器打开 http://127.0.0.1:8000/  （前端由 FastAPI 挂载在同源根路径，
#    不能直接双击 index.html——相对路径的 /api 请求会解析失败）
#    例："统计每种水果的总销量，并画一张柱状图对比"
```

## MySQL 直连

页面底部「连接数据库」按钮，或直接调接口：

```http
POST /api/connect_mysql   {"url": "mysql+pymysql://root:pwd@localhost:3306/stdw"}
```

行数 ≤ `DATA_AGENT_LAZY_LOAD_LIMIT`（默认 50 万）的表搬入本地 DuckDB，
大表留在 MySQL——查询时 SQL 自动转方言整条下推，结果回流。
接入后 `GET /api/tables` 会带上远端表，Agent 自动路由（冷表下推 / 热表本地）。

连接串含密码，日志做了脱敏（`root:***@host`）；连接是**按需手动**而非启动自动，
因为连接信息属于用户输入、不该硬编码进启动参数。

演示库：`scripts/stdw_init.sql`（建库 + 17 张表 + 1200 行数据，
可用 `mysql -u root -p < scripts/stdw_init.sql` 导入）。

## MCP Server

把 data-agent 的能力以 MCP 工具暴露给任意宿主（IDE / Claude Desktop 等）。
两种接法，**同一时刻只能选一种**：

### 方式 A：HTTP /mcp（推荐，复用 web 服务进程）

```powershell
.venv\Scripts\python -m uvicorn api.server:app --port 8000
```

宿主配置里加一个 URL 即可：

```json
{
  "mcpServers": {
    "data-agent": { "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

HTTP 端点与 `/api/*` 在**同一个进程**里共用同一个 `DataService`：
Web 页面上传的表，MCP 立刻能查；MCP 保存的用户口径，Web 端下一次提问就生效。

### 方式 B：stdio（独立进程）

```powershell
.venv\Scripts\python -m mcp_server
```

```json
{
  "mcpServers": {
    "data-agent": {
      "command": "C:\\path\\to\\data-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "env": { "DEEPSEEK_API_KEY": "sk-xxx" }
    }
  }
}
```

> **为什么两种方式不能同时开**：DuckDB 的数据文件是**进程级独占**的（不是单连接，
> 是单进程）。第二个进程连 `read_only=True` 都打不开，会直接报
> `Cannot open file ...`。所以要么用 A（一个进程两个入口），要么用 B 并先停掉
> web 服务。真撞上了也不会只给英文报错——引擎会翻译成
> "另一个进程正在使用它……请只保留一个入口"。

暴露 12 个工具（9 个只读 + 3 个写）：

| 工具 | 说明 |
|---|---|
| `list_tables` | 列出当前已注册的所有数据表 |
| `query_sql` | 执行只读 SQL（AST 安全校验 + 表白名单），返回 CSV，可截断 |
| `describe_table` | 查看单表列结构 + 每列类型/缺失值/唯一值概况 |
| `preview_table` | 预览单表前几行样例数据（默认 10 行，上限 50） |
| `get_metric_definition` | 查询指标口径（M8 + 用户级）：责任表 + 口径 SQL，明确"不得换表" |
| `ask_data` | 自然语言提问，自动走"结构 → SQL → 校验 → 执行"链路 |
| `list_report_templates` | 列出可用的报告模板（PR2） |
| `build_report` | （写）按模板生成结构化报告包，返回 `spec_path` + `artifacts`；超时返回 `job_id` 供轮询 |
| `get_report` | 查询报告任务状态（`queued/running/succeeded/failed`），进程重启后仍可查 |
| `get_report_spec` | 读取报告完整内容（report.json 原文），供宿主排版，超长截断 |
| `load_file` | （写）把 DATA_DIR 内的 CSV/Excel 加载入库注册为新表，宿主会弹确认 |
| `save_metric_definition` | （写）保存/覆盖用户自定义指标口径到 data/warehouse/user_metrics.md，跨会话沿用 |

注意：MCP stdio 协议要求 stdout 只走 JSON-RPC，本项目日志已全部重定向到
stderr，不会污染协议通道。`ask_data` 需要 `DEEPSEEK_API_KEY`；纯 SQL 查询
（`query_sql`）不需要 LLM。`/mcp` 的 Host 白名单默认只放行本机
（可用 `DATA_AGENT_MCP_ALLOWED_HOSTS` 追加）。

> **排查"宿主面板里没有新工具"**：打开 `http://127.0.0.1:8000/api/mcp/tools`。
> 返回里有 `build_report` = 新代码已生效；只有 8 个 = 进程没重启（工具清单是宿主连接时缓存的）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 自然语言分析必填（懒加载：不配也能启动/上传/看表） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | api.deepseek.com / deepseek-chat | 模型端点 |
| `DATA_AGENT_DB` | data/warehouse/analysis.duckdb | 落盘位置（None=纯内存） |
| `DATA_AGENT_QUERY_TIMEOUT` | 30 | 单查询秒数上限（超时用 `conn.interrupt()` 真打断） |
| `DATA_AGENT_MAX_QUERY_ROWS` | 100000 | 单查询行数上限 |
| `DATA_AGENT_MYSQL_TIMEOUT_MS` | 30000 | MySQL 下推查询超时（`MAX_EXECUTION_TIME`） |
| `DATA_AGENT_MYSQL_POOL_RECYCLE` | 3600 | MySQL 连接最大存活秒数（防服务端 wait_timeout 断连） |
| `DATA_AGENT_LLM_TIMEOUT` | 30 | LLM 调用秒数上限 |
| `DATA_AGENT_MAX_UPLOAD` | 50MB | 上传文件大小上限（边读边计数，超限 413） |
| `DATA_AGENT_LAZY_LOAD_LIMIT` | 500000 | 冷热表分流行数阈值 |
| `DATA_AGENT_REACT_STEPS` | 8 | ReAct 循环最大步数 |
| `DATA_AGENT_MAX_PLOT_ROWS` | 200 | 图表最大数据点数 |
| `DATA_AGENT_MCP_ALLOWED_HOSTS` | 127.0.0.1, localhost, `[::1]`（含任意端口） | `/mcp` 的 Host 白名单（防 DNS rebinding） |
| `DATA_AGENT_HITL` | 1 | 高危 SQL 人工确认开关（0 = 回到"生成即执行"） |
| `DATA_AGENT_HITL_MAX_SCAN_ROWS` | 1000000 | 预估扫描行数超过该值 → 需要确认 |
| `DATA_AGENT_HITL_PENDING_TTL` | 1800 | 待确认请求存活秒数（超时后重连不再回放） |
| `DATA_AGENT_HITL_MAX_PENDING` | 50 | 同时挂起的待确认上限 |

## 高危 SQL 人工确认（HITL）

数据分析 Agent 最危险的失败不是"报错"，而是**算出一个看起来很合理的错数**。
执行前把 SQL 摆到用户面前，是唯一能同时拦住"打爆库"和"口径漂移"的关卡。

一回合的真实事件序列（`tests/test_hitl.py` 的等价流程）：

```
start
node  react_agent  决定调用 query_sql
node  plan_sql     已生成查询计划（预估扫描 500 行）
approval_required  thread=demo:3526fe02  sql=SELECT * FROM big
                    - 预估扫描 500 行，超过阈值 100 行
                    - SELECT * 且没有 WHERE 条件（可能把整表拉回内存）
done                     ← 这一轮在这里暂停，历史还没写
```

用户点"批准"后，服务端用 `Command(resume=...)` 从断点继续**同一张图**：

```
start
node  guard        用户已确认，开始执行
tool  query_sql    ok=True 共 500 行
node  tool_execute
node  react_agent
answer / history / done
```

四条拦截规则（全部在 [data/risk.py](data/risk.py)，纯函数、可单测）：

| 规则 | 判定 | 拦的是什么 |
|---|---|---|
| R1 大表扫描 | 引用表基数之和 > `HITL_MAX_SCAN_ROWS` | 卡死 worker / OOM |
| R2 无边界投影 | `SELECT *` 且无 `WHERE` | LLM 偷懒把整表拉回 |
| R3 远端下推 | 命中留在 MySQL 的大表 | 打生产库 |
| R4 口径偏离 | 命中指标，但 SQL 表 ∉ 责任表 | 最隐蔽的"数算错了" |

普通聚合查询（`count(*)` / `sum + WHERE`）**零打扰**，不会弹卡片——
每条都问的话，用户第三次就会无脑点"同意"，审批等于没有。

设计细节。踩坑记录与面试讲法见 [HITL改造记录-2026-10-30.md](HITL改造记录-2026-10-30.md)。

## 报告流水线（给 WorkBuddy 的渲染包）

报表不走聊天循环——**模板 + SQL 是确定性的，不需要 LLM**；LLM 只负责临时问答。

```powershell
python -m data.report.run --list-templates
python -m data.report.run --template monthly_ecom --period 2026-10
```

产物（`data/warehouse/report_jobs/{job_id}/`）：

| 文件 | 作用 |
|---|---|
| `report.json` | **唯一事实源**：metrics（数字）/ tables / charts（文件路径）/ caveats（口径）/ queries |
| `assets/*.png` + `*.json` | 图表 + 图表背后的原始数据（供对方二次排版） |
| `status.json` | `status/stage/progress/error`，进程重启后仍可查 |

四条契约（对方渲染时不能破）：

1. 数字是 `number`，叙述与数字分离，渲染方**只排版不改值**；
2. 图表是文件路径，不是 base64；
3. 口径随包走（`definition` / `caveats` / `meta.queries`）；
4. 失败是 `status=failed` + `error.code`，不是一段会被排进 PPT 的文字。

口径可以完全不写 SQL：`monthly_ecom_locked` 模板里只写 `metric: gmv`，SQL 从
`skills/metrics` 取，改口径只改 references——M4/M8 的口径治理终于接到了报告层。

演示数据：`python scripts/seed_report_demo.py`（幂等补 2026-09 + 10 月后半月，`--clean` 回滚），
补完就能看到真实环比。演示分镜见 [演示脚本-60秒.md](演示脚本-60秒.md)。
设计文档：[WorkBuddy集成最终方案-2026-11-01.md](WorkBuddy集成最终方案-2026-11-01.md)。

## 测试

```powershell
.venv\Scripts\python -m pytest -q     # 331 passed
```

全部通过 fake LLM / SQLite 注入验证，**不需要 `DEEPSEEK_API_KEY`**：
`api.server` 的 import 不再构造 OpenAI 客户端（以前没有 key 会在 collection
阶段就报 `Missing credentials`，CI 必红）。
覆盖的高危用例：SQL 注入（分号多语句、危险函数）、路径穿越、
超时/行数护栏与中断后锁释放、并发注册、指标口径锁表、跨轮事件不丢失、断线重试去重、
技能加载 fail-closed（缺失/非法 frontmatter 不上架）与工具收窄不扩权、
上传超限 413、`/mcp` 与 HTTP 共用上下文、同会话并发串行、用户级口径 Web/MCP 双向可见、
HITL（高风险必拦且不执行 / 批准执行同一条 SQL / 拒绝理由回喂 / 低风险零打扰 /
重复审批 409 / 断线回放卡片）、报告流水线（契约校验 / 模板缺表降级 / 幂等缓存 /
日期与 NaN 的 JSON 安全 / 图表落盘）。

> 注意：服务在跑时不能同时跑测试——DuckDB 是单进程独占，会抢文件锁。
> 先停服务再跑 `pytest`（或 `DATA_AGENT_DB=None` 用纯内存库）。

## 里程碑与版本记录

| 版本 | 交付物 | 关键点 |
|---|---|---|
| M1 | DuckDB 统一引擎 + 文件加载 | pandas→DuckDB 注册三步骤 |
| M2 | sqlglot AST 六步校验 | 正则挡不住注入，AST 可以 |
| M3 | LangGraph 四节点主链路 | 必须显式声明所有边（否则节点被剪掉） |
| M4 | 指标口径字典 | 口径是数仓核心资产 |
| M5a | MySQL 冷热分层 | 方言转译 + 行数采样 |
| M5b/M5c | 多轮对话 + FastAPI + SSE + 前端 | 无状态图 + 有状态后端 |
| M6 | ReAct 多工具循环 | LangGraph 1.x state 全量替换的坑 |
| M7 | 图表可视化（plot_chart 工具） | 结构化数据传递优于文本搬运 |
| M8 | 指标 Skill 最小实现 | 口径配置化（markdown）；渐进式披露 + 工具白名单收窄；解析失败 fail-closed |
| M9 | MCP Server | 8 个工具暴露给宿主；纯套壳复用校验/执行；日志走 stderr 保护协议通道 |
| 硬化 | 查询护栏 / 结构化日志 / LLM 超时 / 数据落盘 / 断线重连 / CI | 8 项生产级补课 |
| **P0 修复** | 见 [P0修复记录-2026-10-30.md](P0修复记录-2026-10-30.md) | 单进程双入口(MCP+Web) / 无 key 可启动 / 不阻塞事件循环 / 上传真限流 / 超时真中断 / 指标注册表统一 |
| **Sprint 0/1** | 见 [HITL改造记录-2026-10-30.md](HITL改造记录-2026-10-30.md) | LangGraph 1.x 语义加固（TypedDict + reducer）；高危 SQL 人工确认（plan → guard → execute + interrupt/断点恢复） |
| **报告 PR1** | 见 [报告流水线-PR1-说明.md](报告流水线-PR1-说明.md) | ReportSpec 契约（数字不可篡改）+ 模板化报告流水线（LangGraph 4 节点）+ 图表/状态落盘 + CLI |
| **报告 PR2** | 见 [报告流水线-PR2-说明.md](报告流水线-PR2-说明.md) | 3 个 MCP 工具 + job 语义（提交/轮询/幂等/stale）+ 状态文件持久化；WorkBuddy 可一句话出报告包 |
| **报告 PR3** | 见 [报告流水线-PR3-说明.md](报告流水线-PR3-说明.md) | WorkBuddy 技能（report-producer）：固定流程 + 八条铁律 + 页序映射；新增 `get_report_spec` |
| **报告 PR4** | 见 [报告流水线-PR4-说明.md](报告流水线-PR4-说明.md) | 数据覆盖度（`meta.warnings` + `sections[].coverage`）+ 数字精度（浮点去噪）；真机跑通并回收问题 |
| **报告 PR5** | 见 [报告流水线-PR5-说明.md](报告流水线-PR5-说明.md) | 环比/同比由数据侧算（区间规则、基期 0、缺失给 reason），渲染方只展示 |
| **报告 PR1** | 见 [WorkBuddy集成最终方案-2026-11-01.md](WorkBuddy集成最终方案-2026-11-01.md) | ReportSpec 契约（数字不可篡改）+ 模板化报告流水线（LangGraph 4 节点）+ 图表/状态落盘 + CLI |
| 最新 | 口径锁表 + 工具调用可视化 + 逐字流式 | 指标漂移治理与可观测性 |

## 技术栈

Python 3.11+ · LangGraph · DuckDB · sqlglot · FastAPI · pandas · matplotlib · SQLAlchemy · DeepSeek API
