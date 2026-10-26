"""sqlglot AST 级 SQL 安全校验（M2）。

为什么放弃 M1 的"关键字黑名单"快闸（_is_read_only）：
  1. 误杀：SELECT update_time ... 或 WHERE note = 'please DELETE' 会被
     黑名单误拦，尽管它们完全只读。
  2. 绕过：DuckDB 允许 SELECT read_csv('C:/secret.txt') 直读任意文件、
     ATTACH 外部数据库、INSTALL/LOAD 扩展、COPY ... TO ... 写文件——
     这些语句都以 SELECT 开头，且不含被禁关键字。

AST 校验 = 把 SQL 解析成语法树，按"结构"判断，而不是按"字符串"猜。
策略（从 Data-Analysis-Agent 的 validate.py 借鉴并精简）：
  - 只接受单条 SELECT / WITH / UNION 语句（拒绝分号多语句注入）
  - 递归扫描危险函数（写文件/扩展/外部库/网络/元命令）
  - 文件读取函数（read_csv 等）做路径白名单：只允许读 config.DATA_DIR
  - 拒绝 COPY ... TO 这类把数据写出库的操作

与 engine.py 的连接级封锁（SET enable_external_access = false）分工：
  - 本模块是"规则闸"：按 AST 判断 SQL 是否可疑（能放行白名单内 read_csv）
  - 连接封锁是"物理闸"：DuckDB 本身禁止一切文件/网络访问（read_csv 执行必失败）
  即使 AST 漏判，物理闸兜底；即使有人关掉物理闸，AST 白名单仍在。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import sqlglot
import sqlglot.expressions as exp

import config

# 危险函数：无论出现在 SQL 哪里都必须拒绝。
# 这些函数能碰文件系统写入 / 扩展加载 / 外部库 / 网络 / 元命令。
_BANNED_FUNCTIONS = {
    "copy_to",                       # 把查询结果写进文件
    "install", "load",               # 加载扩展（可执行任意原生代码）
    "attach", "detach",              # 挂载/卸载外部数据库
    "pragma",                        # DuckDB 元命令（可改连接设置）
    "http_get", "http_get_blob",     # 网络读取（SSRF）
    "http_post",
}

# 文件读取函数：本身不危险，但路径要受白名单约束。
# 数据来源应该是"用户已注册的表"，LLM 不应该乱读文件。
_FILE_READ_FUNCTIONS = {
    "read_csv", "read_csv_auto",
    "read_parquet", "read_parquet_auto",
    "read_json", "read_json_auto",
    "read_text", "read_text_auto",
    "read_blob", "read_blob_auto",
}

# sqlglot 会为 read_csv/read_parquet/read_json 生成专用节点类型（ReadCSV 等）。
# 其余 read_* 函数解析为通用 Anonymous 节点。两者都要拦截。
_FILE_READ_NODE_TYPES = {"readcsv", "readparquet", "readjson"}

# 网络 URL 前缀：路径参数带这些一律拒绝（防 SSRF / 外部数据泄露）
_BLOCKED_URL_SCHEMES = (
    "http://", "https://", "ftp://", "ftps://",
    "s3://", "gs://", "gcs://", "azure://", "abfs://", "abfss://",
    "hdfs://", "webhdfs://", "sftp://", "scp://",
)

# 通配符字符：read_csv('data/*.csv') 这类 glob 路径，校验其父目录
_GLOB_CHARS = ("*", "?", "[", "]")


def validate_sql(
    sql: str,
    allowed_roots: Optional[list[Path]] = None,
    allowed_tables: Optional[list[str]] = None,
) -> Optional[str]:
    """校验一条 SQL，返回错误描述字符串；合法返回 None。

    allowed_roots: 文件读取函数允许访问的根目录列表。
      默认 = config.DATA_DIR（上传文件落盘目录），LLM 只能读这里。
    allowed_tables: 表名白名单（M4）。非 None 时，SQL 引用的物理表
      必须全部在此集合内（CTE 别名除外）。默认 None = 不限制。
    """
    # ── 1. 解析成语法树 ──────────────────────────────────────────────
    # dialect="duckdb"：按 DuckDB 语法解析（read_csv 等函数才能正确识别）
    # RAISE：遇到解析不了的 SQL 直接抛异常（说明语法本身就不合法）
    try:
        statements = sqlglot.parse(
            sql, dialect="duckdb", error_level=sqlglot.ErrorLevel.RAISE
        )
    except Exception as exc:
        return f"SQL 解析失败（语法不合法）: {exc}"

    # ── 2. 多语句检查 ────────────────────────────────────────────────
    # sqlglot.parse 会按分号切出多条语句。超过 1 条 = 分号注入，拒绝。
    # 这堵住了 M1 快闸唯一的漏洞：SELECT 1; DROP TABLE x
    if len(statements) > 1:
        return "不允许多语句 SQL（检测到分号分隔的多条语句）。"
    if not statements:
        return "SQL 语句为空。"

    stmt = statements[0]

    # ── 3. 顶层语句类型检查 ──────────────────────────────────────────
    # exp.Query 是"只读查询"的公共父类：SELECT / WITH(CTE) / UNION 都是它。
    # INSERT / UPDATE / DELETE / DROP / CREATE 等不继承 Query，直接拒绝。
    if not isinstance(stmt, exp.Query):
        return (
            f"只允许 SELECT / WITH / UNION 查询，检测到: {type(stmt).__name__}。"
            "请使用 SELECT 语句查询数据。"
        )

    # ── 4. 递归扫描危险函数 ──────────────────────────────────────────
    # stmt.walk() 遍历整棵语法树的所有节点（含子查询/函数参数内部）
    for node in stmt.walk():
        # Anonymous：sqlglot 没专门建模的通用函数调用（如 copy_to(...)）
        if isinstance(node, exp.Anonymous):
            fname = (node.name or "").lower()
            if fname in _BANNED_FUNCTIONS:
                return f"禁止使用函数 {fname}()：可访问文件系统写入/网络/扩展。"

        # Func 及子类：sqlglot 专门建模的函数（如 pragma_version() 是 Func）
        if isinstance(node, exp.Func):
            fname = type(node).__name__.lower()
            sql_name = getattr(node, "sql_name", lambda: "")()
            for candidate in (fname, str(sql_name).lower()):
                if candidate in _BANNED_FUNCTIONS:
                    return f"禁止使用函数 {candidate}()：可访问文件系统写入/网络/扩展。"

    # ── 5. COPY 节点扫描 ─────────────────────────────────────────────
    # COPY ... TO 'file' 会把表数据写出库，必须拒绝
    for node in stmt.walk():
        if isinstance(node, exp.Copy):
            return "禁止 COPY 操作：不允许把数据写入文件。"

    # ── 6. 文件读取函数路径白名单 ────────────────────────────────────
    err = _validate_file_read_paths(stmt, allowed_roots)
    if err:
        return err

    # ── 7. 表名白名单（M4）────────────────────────────────────────────
    # 放在路径白名单之后：带引号的"表名看起来像文件路径"的 SQL
    # （SELECT * FROM 'secret.txt'）优先报路径错误，行为与 M2 保持兼容。
    if allowed_tables is not None:
        err = _validate_table_whitelist(stmt, allowed_tables)
        if err:
            return err

    return None  # 全部检查通过


# ---------- 公共函数：收集 SQL 引用的物理表（白名单 + 路由共用） ----------

def collect_referenced_tables(stmt) -> set[str]:
    """返回 SQL 引用的物理表名集合（已剔除 CTE 别名）。

    白名单校验（M4）和路由判定（M5a 小表本地/大表下推）都要用
    "这条 SQL 到底碰了哪些表"，抽成公共函数避免两处逻辑漂移。
    CTE 别名不算物理表：WITH x AS (...) SELECT * FROM x 里的 x
    是查询内部定义的临时名。
    """
    # CTE 别名集合：WITH 子句里定义的临时名（casefold，大小写不敏感）
    cte_aliases: set[str] = set()
    for cte in stmt.find_all(exp.CTE):
        alias = cte.alias or cte.args.get("alias")
        if alias:
            cte_aliases.add(str(alias).casefold())

    referenced: set[str] = set()
    for table in stmt.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        if str(name).casefold() in cte_aliases:
            continue
        referenced.add(str(name))
    return referenced


# ---------- 文件读取路径白名单（内部实现） ----------

def _validate_table_whitelist(stmt, allowed_tables: list[str]) -> Optional[str]:
    """表级白名单：SQL 引用的物理表必须全部在 allowed_tables 内。

    关键点：CTE 别名不算物理表。WITH x AS (...) SELECT * FROM x 里
    的 x 是查询内部定义的临时名，必须从引用集合里剔除，否则会误杀
    合法 CTE 查询。
    大小写不敏感：表名统一 casefold 比较（DuckDB 表名本身不敏感）。
    """
    allowed = {str(t).casefold() for t in allowed_tables}

    # CTE 别名集合：WITH 子句里定义的临时名
    cte_aliases: set[str] = set()
    for cte in stmt.find_all(exp.CTE):
        alias = cte.alias or cte.args.get("alias")
        if alias:
            cte_aliases.add(str(alias).casefold())

    offenders: set[str] = set()
    for table in stmt.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        folded = str(name).casefold()
        if folded in cte_aliases:
            continue
        if folded not in allowed:
            offenders.add(str(name))

    if offenders:
        available = ", ".join(sorted(allowed_tables)) or "（无）"
        return (
            "引用了未注册的表: " + ", ".join(sorted(offenders))
            + f"（当前可用表: {available}）"
        )
    return None


def _validate_file_read_paths(stmt, allowed_roots: Optional[list[Path]]) -> Optional[str]:
    """扫描 AST，对所有文件读取函数的路径做白名单校验。"""
    # 标准化白名单根（resolve 成绝对路径，才能做前缀匹配）
    roots = [config.DATA_DIR] if not allowed_roots else list(allowed_roots)
    norm_roots: list[Path] = []
    for r in roots:
        try:
            norm_roots.append(Path(r).expanduser().resolve())
        except (OSError, RuntimeError):
            continue

    for node in stmt.walk():
        paths: list[str] = []
        all_literal = True

        # 情况 A：专用节点 ReadCSV / ReadParquet / ReadJSON
        node_type = type(node).__name__.lower()
        if node_type in _FILE_READ_NODE_TYPES:
            paths, all_literal = _extract_path_literals(node)
        # 情况 B：Anonymous 节点（read_csv_auto / read_text / read_blob 等）
        elif isinstance(node, exp.Anonymous) and (node.name or "").lower() in _FILE_READ_FUNCTIONS:
            paths, all_literal = _extract_path_literals(node)
        # 情况 C：DuckDB 的 SELECT * FROM 'data.csv' 文件扫描，
        #         sqlglot 解析成带引号的 Table 而非 ReadCSV，也要走白名单
        elif isinstance(node, exp.Table):
            name = str(getattr(node, "name", "") or "")
            quoted = bool(getattr(getattr(node, "this", None), "args", {}).get("quoted"))
            if quoted and _looks_like_file(name):
                paths, all_literal = [name], True
        else:
            continue  # 普通节点，跳过

        if not paths:
            if not all_literal:
                return f"禁止非字面量路径参数：文件读取函数的路径必须是字符串字面量。"
            continue

        for path_str in paths:
            err = _check_single_path(path_str, norm_roots)
            if err:
                return err

    return None


def _extract_path_literals(node) -> tuple[list[str], bool]:
    """从文件读取函数节点提取字面量路径参数。

    返回 (paths, all_literal)：
      - paths: 提取到的字面量路径
      - all_literal: False 表示存在非字面量参数（@var/列引用/拼接），应拒绝
    """
    paths: list[str] = []
    all_literal = True

    # ReadCSV/ReadParquet/ReadJSON：第一个参数在 node.this
    candidates: list = []
    if getattr(node, "this", None) is not None:
        candidates.append(node.this)
    # Anonymous：node.expressions 是所有参数，第一项是路径
    if type(node).__name__ == "Anonymous" and node.expressions:
        candidates = [node.expressions[0]]

    for cand in candidates:
        # 单个字符串字面量：read_csv('data/a.csv')
        if isinstance(cand, exp.Literal) and cand.is_string:
            paths.append(cand.this)
        # 数组字面量：read_csv(['a.csv', 'b.csv'])
        elif isinstance(cand, exp.Array):
            for item in cand.expressions or []:
                if isinstance(item, exp.Literal) and item.is_string:
                    paths.append(item.this)
                else:
                    all_literal = False
        # 其他（Column/Parameter/拼接表达式）→ 非字面量
        else:
            all_literal = False

    # 有参数但一个字面量都没提到 → 全是非字面量
    if candidates and not paths:
        all_literal = False

    return paths, all_literal


def _looks_like_file(value: str) -> bool:
    """判断带引号的 Table 名看起来像文件路径（SELECT * FROM 'data.csv'）。"""
    lowered = str(value or "").strip().lower()
    return (
        "/" in lowered or "\\" in lowered
        or lowered.endswith((".csv", ".tsv", ".parquet", ".json", ".jsonl",
                             ".xlsx", ".xls", ".txt"))
    )


def _check_single_path(path_str: str, norm_roots: list[Path]) -> Optional[str]:
    """校验单个路径字符串是否在白名单根目录内。"""
    if not path_str:
        return "文件读取函数的路径参数为空。"

    # ── 网络 URL 一律拒绝（SSRF 防护）──────────────────────────────
    lowered = path_str.lower().strip()
    for scheme in _BLOCKED_URL_SCHEMES:
        if lowered.startswith(scheme):
            return f"禁止网络路径：{path_str}（只允许本地目录内文件）。"

    # ── 解析成绝对路径 ──────────────────────────────────────────────
    try:
        candidate = Path(path_str)
        if candidate.is_absolute():
            resolved = candidate.expanduser().resolve(strict=False)
        else:
            # 相对路径：基于第一个白名单根解析（通常是 config.DATA_DIR）
            resolved = (norm_roots[0] / path_str).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"路径解析失败：{path_str}（{exc}）"

    # glob 路径（read_csv('data/*.csv')）校验父目录
    has_glob = any(c in path_str for c in _GLOB_CHARS)
    check_path = resolved.parent if has_glob else resolved

    # ── 白名单前缀匹配 ──────────────────────────────────────────────
    for root in norm_roots:
        try:
            check_path.relative_to(root)  # 在 root 内则抛 ValueError，这里不抛
            return None
        except ValueError:
            continue
    return (
        f"路径不在允许的目录内：{path_str}（允许的根目录: "
        + ", ".join(str(r) for r in norm_roots) + "）。"
    )
