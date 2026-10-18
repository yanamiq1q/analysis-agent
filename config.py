"""单一配置源：所有阈值/路径/常量集中在这里，禁止散落硬编码。

从 Data-Analysis-Agent 学到的教训：阈值如果散落多处，容易改一处漏一处。
"""
import os
from pathlib import Path

# ---- 项目路径 ----
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "warehouse"   # DuckDB 文件/上传文件落盘位置
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---- DuckDB ----
# None = 纯内存模式（进程结束数据消失）；指定路径 = 持久化到文件
DUCKDB_PATH: str | None = os.getenv("DATA_AGENT_DB", str(DATA_DIR / "analysis.duckdb"))

# ---- 数据加载 ----
# 上传文件大小上限（字节）
MAX_UPLOAD_BYTES = int(os.getenv("DATA_AGENT_MAX_UPLOAD", str(50 * 1024 * 1024)))  # 50MB
# 允许的上传扩展名
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".parquet"}

# ---- 大表下推阈值 ----
# 连接远端库时：超过该行数的表不拉进 DuckDB，SQL 整条下推远端执行（M5a 启用）
LAZY_LOAD_ROW_LIMIT = int(os.getenv("DATA_AGENT_LAZY_LOAD_LIMIT", "500000"))

# ---- 单文件大小上限（防御：避免一个文件把所有东西都塞进去）----
MAX_SOURCE_TABLES = 20

# ---- LLM（DeepSeek，openai 兼容协议）----
# API key 必须从环境变量注入，禁止硬编码进代码
DEEPSEEK_API_KEY: str | None = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
# 单次 LLM 调用超时（秒）：openai SDK 的 timeout 同时约束连接与读取，
# 防网络抖动导致请求永久挂起（graph 层会捕获并走统一重试链路）
LLM_TIMEOUT_SECONDS = float(os.getenv("DATA_AGENT_LLM_TIMEOUT", "30"))

# ---- Agent（M3）----
# SQL 生成失败（校验/执行出错）的最大重试次数
MAX_SQL_RETRIES = 3
# 最终答案展示给用户的最大行数
MAX_RESULT_ROWS = 20

# ---- 查询护栏（生产级硬伤修复）----
# 单次查询最大执行秒数：防止 LLM 生成的霸王 SQL（笛卡尔积/全表扫）
# 卡死 worker。DuckDB 侧用线程池超时；MySQL 侧用 MAX_EXECUTION_TIME。
QUERY_TIMEOUT_SECONDS = float(os.getenv("DATA_AGENT_QUERY_TIMEOUT", "30"))
# 单次查询最大返回行数：超过直接报错，防止把整张表拉进内存
MAX_QUERY_ROWS = int(os.getenv("DATA_AGENT_MAX_QUERY_ROWS", "100000"))
# MySQL 下推查询超时（毫秒，仅作用于 SELECT，8.0 特性）
MYSQL_MAX_EXECUTION_TIME_MS = int(os.getenv("DATA_AGENT_MYSQL_TIMEOUT_MS", "30000"))
# MySQL 连接池断线重连：连接存活超过该秒数强制回收（pool_recycle），
# 必须明显小于 MySQL wait_timeout（默认 28800s），让连接主动轮换
# 而不是被服务端被动断开；配合 pool_pre_ping 探活实现无感重连
MYSQL_POOL_RECYCLE_SECONDS = int(os.getenv("DATA_AGENT_MYSQL_POOL_RECYCLE", "3600"))
