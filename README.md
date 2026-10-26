# data-agent

个人数据分析 Agent：自然语言提问 → 自动查数据 → 生成答案（含图表）。
基于 **LangGraph + DuckDB**，参照开源项目 Data-Analysis-Agent 精读后自研，
重点补齐了生产级防线（SQL 安全校验、查询护栏、结构化日志、断线重连）。

## 核心能力

- **统一查询引擎**：CSV / Excel / JSON / Parquet / MySQL 全部接入 DuckDB，一个引擎跑所有 SQL
- **ReAct 多工具循环**：LLM 自主决策——先查表清单，再分组统计，需要时自动画图，最后综合成答案
- **工具调用可视化**：每步工具调用（工具名 / 参数 / 结果摘要）渲染成步骤卡片推给前端，
  文字逐字流出——用户能看见 Agent 在做什么，而不是干等
- **指标口径字典**：内置 GMV、订单量等标准口径，命中即**锁定责任表**，禁止换表或用汇总表兜底
- **技能包机制（skill）**：指标口径是 `skills/metrics` 下的 markdown 文件（改口径零代码、不重新发版）；
  系统提示只带一行技能目录（渐进式披露省 token），`load_skill` 激活后注入全文并**按技能
  allowedTools 收窄工具集**（最小权限，只收窄不扩权）
- **MySQL 冷热分层**：小表搬入本地、大表 SQL 整条下推远端执行（sqlglot 方言转译 + 行数采样判定）
- **MCP Server（M9）**：向 IDE / Claude Desktop 等宿主暴露 3 个只读工具——`list_tables` / `query_sql`（原始 SQL）/ `ask_data`（自然语言子问题），纯套壳复用现有校验与执行链路
- **安全四道防线**：sqlglot AST 白名单校验 → DuckDB 连接级封锁 → 行数/超时护栏 → LLM 超时+重试闭环
- **多轮对话**：会话记忆 + SSE 流式输出 + 断线自动重连

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
| 外层 ReAct | `agent/react.py` | 看对话上下文决定调哪个工具 / 何时收尾 |
| 内层 SQL 链 | `agent/graph.py` | collect_schema → generate_sql → validate_sql → execute，含校验/护栏/重试 |

SSE 事件（前端逐条消费）：

| 事件 | 内容 |
|---|---|
| `node` | 节点完成；`react_agent` 带真实决策文案（如「决定调用 query_sql」），其余为空串 |
| `tool_call` | 工具名 + 参数原文 + 结果摘要（前端渲染成步骤卡片） |
| `chart` | base64 PNG 图表 |
| `answer` | 最终答案 |
| `history` | 追加本轮问答后的完整会话历史 |

## 目录结构

```
data-agent/
├── agent/            # Agent 层：graph(四节点) / react(ReAct) / tools(工具集) / llm / skill_loader(技能加载)
├── data/             # 数据层：engine(DuckDB) / service(路由) / validate(sqlglot) / metrics(指标匹配逻辑)
│   └── sources/      # 数据源：loaders(文件) / mysql_source(MySQL 冷热分层)
├── skills/           # 技能包：metrics/SKILL.md + references/metrics.md（指标口径，markdown 配置化）
├── api/server.py     # FastAPI：chat(SSE) / upload / tables / connect_mysql
├── mcp_server/       # MCP Server（M9）：list_tables / query_sql / ask_data（python -m mcp_server）
├── frontend/         # 原生 HTML 前端（fetch+getReader 手写 SSE 消费 + 打字机渲染）
├── scripts/          # stdw_init.sql：演示库 DDL（含 1200 行数据生成）
├── tests/            # 225 个测试，全 fake 注入，无需真实 key/MySQL
└── config.py         # 单一配置源：全部阈值集中于此
```

## 快速开始

```powershell
# 1. 安装依赖（Python 3.11+）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置环境变量（必须）
$env:DEEPSEEK_API_KEY = "sk-xxx"          # DeepSeek API key（openai 兼容）

# 3. 启动服务
.venv\Scripts\python -m uvicorn api.server:app --port 8000

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

把 data-agent 的能力以 MCP 工具暴露给任意宿主（IDE / Claude Desktop 等），
stdio 模式启动：

```powershell
.venv\Scripts\python -m mcp_server
```

暴露 3 个工具（全部标注只读）：

| 工具 | 说明 |
|---|---|
| `list_tables` | 列出当前已注册的所有数据表 |
| `query_sql` | 执行只读 SQL（AST 安全校验 + 表白名单），返回 CSV，可截断 |
| `ask_data` | 自然语言提问，自动走"结构 → SQL → 校验 → 执行"链路 |

在宿主（如 Claude Desktop / VS Code）的 mcp 配置里加一条：

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

注意：MCP stdio 协议要求 stdout 只走 JSON-RPC，本项目日志已全部重定向到
stderr，不会污染协议通道。`ask_data` 需要 `DEEPSEEK_API_KEY`；纯 SQL 查询
（`query_sql`）不需要 LLM。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填，LLM API key |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | api.deepseek.com / deepseek-chat | 模型端点 |
| `DATA_AGENT_DB` | data/warehouse/analysis.duckdb | 落盘位置（None=纯内存） |
| `DATA_AGENT_QUERY_TIMEOUT` | 30 | 单查询秒数上限 |
| `DATA_AGENT_MAX_QUERY_ROWS` | 100000 | 单查询行数上限 |
| `DATA_AGENT_MYSQL_TIMEOUT_MS` | 30000 | MySQL 下推查询超时（`MAX_EXECUTION_TIME`） |
| `DATA_AGENT_MYSQL_POOL_RECYCLE` | 3600 | MySQL 连接最大存活秒数（防服务端 wait_timeout 断连） |
| `DATA_AGENT_LLM_TIMEOUT` | 30 | LLM 调用秒数上限 |
| `DATA_AGENT_MAX_UPLOAD` | 50MB | 上传文件大小上限 |
| `DATA_AGENT_LAZY_LOAD_LIMIT` | 500000 | 冷热表分流行数阈值 |
| `DATA_AGENT_REACT_STEPS` | 8 | ReAct 循环最大步数 |
| `DATA_AGENT_MAX_PLOT_ROWS` | 200 | 图表最大数据点数 |

## 测试

```powershell
.venv\Scripts\python -m pytest -q     # 225 passed
```

全部通过 fake LLM / SQLite 注入验证，不依赖真实 API key 与 MySQL。
覆盖的高危用例：SQL 注入（分号多语句、危险函数）、路径穿越、
超时/行数护栏、并发注册、指标口径锁表、跨轮事件不丢失、断线重试去重、
技能加载 fail-closed（缺失/非法 frontmatter 不上架）与工具收窄不扩权。

> 注意：服务在跑时不能同时跑测试——DuckDB 是单写入者，会抢文件锁。
> 先停服务再跑 `pytest`。

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
| M9 | MCP Server | 3 个只读工具暴露给宿主；纯套壳复用校验/执行；日志走 stderr 保护协议通道 |
| 硬化 | 查询护栏 / 结构化日志 / LLM 超时 / 数据落盘 / 断线重连 / CI | 8 项生产级补课 |
| 最新 | 口径锁表 + 工具调用可视化 + 逐字流式 | 指标漂移治理与可观测性 |

## 技术栈

Python 3.11+ · LangGraph · DuckDB · sqlglot · FastAPI · pandas · matplotlib · SQLAlchemy · DeepSeek API
