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
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.graph import build_graph
from agent.llm import build_deepseek_generator
from data.service import DataService
from logging_setup import get_logger, setup_logging

logger = get_logger("api.server")
setup_logging()  # 服务入口：初始化根日志（幂等，重复调用无副作用）

app = FastAPI(title="data-agent")

# 模块级单例：Agent 依赖的"后端状态"。llm/service 在测试里被 monkeypatch 替换。
# service 用默认构造 = 落盘到 config.DUCKDB_PATH 的 duckdb 文件：
# 上传的表重启服务后依然可查（测试里用 db_path=None 隔离）。
service = DataService()
llm = build_deepseek_generator()

# 会话历史（有状态后端）：session_id -> [{role, content}, ...]
_sessions: dict[str, list[dict]] = {}

# 每个节点对应的前端进度文案
_NODE_MESSAGES = {
    "collect_schema": "正在读取表结构和指标口径…",
    "generate_sql": "正在生成 SQL…",
    "validate_sql": "正在校验 SQL 安全…",
    "execute": "正在执行查询…",
}


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    retry: bool = False  # 断线重连重发标记：后端据此对历史做幂等去重


def _sse(event: dict) -> str:
    """把一个事件对象编码成一段 SSE 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


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
    graph = build_graph(service, llm)

    final_state: dict = {}
    # updates 模式：每完成一个节点吐一次该节点的状态增量
    for chunk in graph.stream(
        {"question": req.message, "history": history},
        stream_mode="updates",
    ):
        node_name, update = next(iter(chunk.items()))
        final_state.update(update)
        yield _sse({
            "type": "node",
            "node": node_name,
            "message": _NODE_MESSAGES.get(node_name, node_name),
        })

    answer = final_state.get("result_text") or final_state.get("error") or "（无输出）"

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