"""FastAPI 服务（M5c）：SSE 流式对话端点 + 极简前端。

设计（对齐旧项目 api/chat.py 的 SSE 实现，取其"无状态 Agent + 有状态后端"分工）：

  - Agent 图无状态：每次请求新建图、注入请求体里的 history。
  - 后端有状态：模块级 sessions dict 保存各会话历史（个人项目用
    内存 dict，旧项目同款做法，重启即清）。
  - 流式：LangGraph 的 stream(updates) 逐节点吐进度事件。旧项目
    是手写 for 循环 yield，我们用框架原生能力等价实现——每完成
    一个节点，前端立刻收到进度行，而不是白屏等整条链路跑完。
  - SSE 协议：data: {json}\\n\\n；响应头禁缓冲（X-Accel-Buffering、
    Cache-Control），这是旧项目踩过的坑（Nginx/浏览器缓冲导致
    "打字机"变成一次性整段到达）。

启动：
  uvicorn api.server:app --port 8000
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.llm import build_deepseek_chat_generator, build_deepseek_generator
from agent.react import build_react_graph, initial_messages
from agent.skill_loader import (
    build_metric_registry,
    list_skill_catalog,
    parse_skill_dir,
)
from agent.tools import TOOL_SCHEMAS
import config
from data.service import DataService
from data.sources.mysql_source import build_mysql_engine
from logging_setup import get_logger, setup_logging

logger = get_logger("api.server")
setup_logging()  # 服务入口：初始化根日志（幂等，重复调用无副作用）

app = FastAPI(title="data-agent")

# 模块级单例：Agent 依赖的"后端状态"。llm/service 在测试里被 monkeypatch 替换。
# service 用默认构造 = 落盘到 config.DUCKDB_PATH 的 duckdb 文件：
# 上传的表重启服务后依然可查（测试里用 db_path=None 隔离）。
service = DataService()
llm = build_deepseek_generator()                          # 内层：问题 → SQL
chat_llm = build_deepseek_chat_generator(TOOL_SCHEMAS)   # 外层：ReAct 调度者

# M8 技能包单例：启动时解析一次 skills/ 目录。个人项目无热重载——
# 改口径文件后重启服务生效（参考项目有热重载，列入 M9 候选）。
# 某个技能解析失败只记日志、不影响其它技能与服务启动（fail-closed
# 到技能级而不是进程级）。
_skills, _skill_diagnostics = parse_skill_dir(config.PROJECT_ROOT / "skills")
for diag in _skill_diagnostics:
    logger.warning("skill load failed path=%s error=%s", diag.path, diag.error)
_metric_registry = build_metric_registry(_skills)   # 内层口径匹配数据源
_skill_catalog = list_skill_catalog(_skills)        # 外层 system 的目录段

# 会话历史（有状态后端）：session_id -> [{role, content}, ...]
_sessions: dict[str, list[dict]] = {}

# 节点进度文案不再用固定占位串：react_agent 节点会产出 step_text
# （本轮真实决策），server 直接把它当 node 事件的 message 推给前端。


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    retry: bool = False  # 断线重连重发标记：后端据此对历史做幂等去重


class MySQLConnectRequest(BaseModel):
    """MySQL 连接请求：url 是完整连接串（含账号密码）。"""
    url: str
    # 冷热分流行数阈值；None 时用 config.LAZY_LOAD_ROW_LIMIT
    lazy_limit: int | None = None


def _mask_url(url: str) -> str:
    """日志脱敏：连接串里有密码，日志只留 host/db，密码永远不进日志。"""
    return re.sub(r"//([^:@/]+):[^@]+@", r"//\1:***@", url)


def _sse(event: dict) -> str:
    """把一个事件对象编码成一段 SSE 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/connect_mysql")
async def connect_mysql(req: MySQLConnectRequest):
    """接入远端 MySQL（M5a）：小表搬进 DuckDB、大表留远端下推。

    连接是"按需手动"而非服务启动时自动——连接串含账号密码，
    属于用户输入而非固定配置，不该硬编码在启动参数里。
    接入后 service.list_tables() 会带上远端表，Agent 的
    query_sql 工具即可自动路由（冷表下推 / 热表本地）。
    """
    t0 = time.monotonic()
    logger.info("connect_mysql start url=%s", _mask_url(req.url))
    try:
        engine = build_mysql_engine(req.url)
        result = service.connect_mysql(engine, lazy_limit=req.lazy_limit)
    except Exception as exc:  # 连不上/权限不足/库不存在：400 而非 500
        logger.warning("connect_mysql failed url=%s error=%s",
                       _mask_url(req.url), exc)
        raise HTTPException(status_code=400, detail=f"连接失败: {exc}") from exc
    logger.info("connect_mysql ok moved=%d large=%d ms=%.1f",
                len(result["moved"]), len(result["large"]),
                (time.monotonic() - t0) * 1000)
    return {
        "moved": result["moved"],   # 已搬进本地引擎的表
        "large": result["large"],   # 留远端下推的大表
        "tables": service.list_tables(),
    }


async def _chat_stream(req: ChatRequest) -> AsyncIterator[str]:
    """对话流生成器：逐节点吐进度事件，最后吐答案和完整历史。"""
    t0 = time.monotonic()
    logger.info("chat start session=%s q=%.80s", req.session_id, req.message)
    # 首个 yield 前先吐"开始"事件：让前端立刻建立 reader 并显示
    # "处理中"，否则浏览器要等第一个真实事件才收流（旧项目同款技巧）
    yield _sse({"type": "start", "message": "开始处理…"})

    history = _sessions.get(req.session_id, [])
    # 断线重连幂等：前端重发同一消息（retry=true）时，把上一轮
    # "答案已生成但传输中断"追加的 [user, assistant] 剥掉，避免
    # 会话历史出现两条相同问答。只剥内容恰好匹配本消息的末尾
    # 两条——用户主动连问两次相同的问题（retry=false）不受影响
    if (
        req.retry
        and len(history) >= 2
        and history[-2].get("role") == "user"
        and history[-2].get("content") == req.message
        and history[-1].get("role") == "assistant"
    ):
        history = history[:-2]
    graph = build_react_graph(service, llm, chat_llm, _metric_registry, _skills)

    final_state: dict = {}
    # charts / tool_events 在 state 里都是"单调累积"，且每个节点 update 都
    # 带全量；各自记录已发数量做差分，避免同一项在后续节点事件里重复推送
    sent = {"charts": 0, "tool_events": 0}
    try:
        # updates 模式：每完成一个节点吐一次该节点的状态增量。
        # ReAct 图中 react_agent/tool_execute 会随循环多轮出现，
        # 前端看到的进度行与 Agent 实际步数一一对应
        for chunk in graph.stream(
            {"messages": initial_messages(req.message, history, _skill_catalog), "steps": 0},
            stream_mode="updates",
        ):
            node_name, update = next(iter(chunk.items()))
            final_state.update(update)

            # 图表产物：base64 data URI，前端直接渲染成图片
            charts = update.get("charts") or []
            for image in charts[sent["charts"]:]:
                yield _sse({"type": "chart", "image": image})
            sent["charts"] = len(charts)

            # 工具调用明细：前端渲染成"Agent 在做什么"的步骤卡片。
            # 这是 ReAct 的"思考过程"——模型调了哪个工具、传了什么
            # 参数、拿到什么结果（deepseek-chat 调工具时不产出推理
            # 文本，动作才是可展示的思考）
            tool_events = update.get("tool_events") or []
            for ev in tool_events[sent["tool_events"]:]:
                yield _sse({"type": "tool_call", **ev})
            sent["tool_events"] = len(tool_events)

            # message 用节点产出的真实内容（react_agent 的决策文本）。
            # tool_execute 没有 step_text → 空串：前端不渲染占位文案，
            # 该步的真实内容由紧随其后的 tool_call 卡片承载
            yield _sse({
                "type": "node",
                "node": node_name,
                "message": update.get("step_text") or "",
            })
    except Exception as exc:
        # 外层 LLM 网络故障/超时等：四节点内层有自己的 fail→重试闭环，
        # 外层没有等价语义，这里统一降级成错误答案而非 500 断流
        logger.exception("chat failed session=%s", req.session_id)
        answer = f"分析服务暂时不可用: {exc}"
        yield _sse({"type": "answer", "text": answer})
        yield _sse({"type": "done"})
        return

    answer = (
        final_state.get("final_answer")
        or final_state.get("last_response", {}).get("content")
        or "（无输出）"
    )

    # 会话历史滚动追加（M5b 的 history 由后端持有）
    new_history = history + [
        {"role": "user", "content": req.message},
        {"role": "assistant", "content": answer},
    ]
    _sessions[req.session_id] = new_history

    logger.info("chat done session=%s ms=%.1f", req.session_id,
                (time.monotonic() - t0) * 1000)
    yield _sse({"type": "answer", "text": answer})
    yield _sse({"type": "history", "history": new_history})
    yield _sse({"type": "done"})


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """SSE 流式对话端点。前端用 fetch + getReader 消费。"""
    return StreamingResponse(
        _chat_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",       # 强制浏览器不缓冲
            "X-Accel-Buffering": "no",         # 强制 Nginx 不缓冲（旧项目踩过的坑）
        },
    )


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """上传数据文件（CSV/Excel/JSON/Parquet）注册进引擎。"""
    t0 = time.monotonic()
    logger.info("upload start file=%s", file.filename)
    suffix = Path(file.filename or "").suffix
    # 落临时文件：loaders 按扩展名分发，临时文件必须保留原后缀
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        # source_name 用用户上传的原始文件名（去扩展名），
        # 不能用临时文件名——那会注册成 tmpm350uj0 这种表名
        source = Path(file.filename or "").stem
        tables = service.load_file(str(tmp_path), source_name=source)
    except Exception as exc:  # 加载失败：报 400 而不是 500 内部错误
        logger.warning("upload failed file=%s error=%s", file.filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)  # 数据已进引擎，临时文件立即清理
    logger.info("upload ok file=%s tables=%s ms=%.1f",
                file.filename, tables, (time.monotonic() - t0) * 1000)
    return {"tables": tables}


@app.get("/api/tables")
async def tables():
    """当前可用表清单（前端状态栏显示用）。"""
    return {"tables": service.list_tables()}


# 静态页面挂载必须放在所有 API 路由之后（mount("/") 只接未被路由匹配的路径）
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")