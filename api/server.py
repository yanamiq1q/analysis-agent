"""FastAPI 服务（M5c）：SSE 流式对话端点 + 极简前端 + 内置 MCP 端点。

设计（对齐旧项目 api/chat.py 的 SSE 实现，取其"无状态 Agent + 有状态后端"分工）：

  - Agent 图无状态：每次请求新建图、注入请求体里的 history。
  - 后端有状态：SessionStore 保存各会话历史（个人项目用内存，重启即清），
    每会话一把锁保证"读历史→跑图→写历史"原子（P0-3）。
  - 流式：LangGraph 的 stream(updates) 逐节点吐进度事件。旧项目
    是手写 for 循环 yield，我们用框架原生能力等价实现——每完成
    一个节点，前端立刻收到进度行，而不是白屏等整条链路跑完。
  - SSE 协议：data: {json}\\n\\n；响应头禁缓冲（X-Accel-Buffering、
    Cache-Control），这是旧项目踩过的坑（Nginx/浏览器缓冲导致
    "打字机"变成一次性整段到达）。

P0 修复（2026-10-30，详见 P0修复记录-2026-10-30.md）：
  - P0-1：``/mcp`` 挂载 FastMCP（同一进程、同一个 DataService），
          不再需要第二个进程去抢 DuckDB 文件锁；
  - P0-2：依赖改为懒加载（app_context.AppContext），没配 API key
          也能启动、上传、看表；
  - P0-3：SSE 端点改同步生成器 → Starlette 在线程池里迭代，不再
          阻塞事件循环；同一会话串行；
  - P0-4：上传分块落盘 + 超限 413，不再先把整个文件读进内存。

启动：
  uvicorn api.server:app --port 8000
  MCP 宿主配置 http://127.0.0.1:8000/mcp 即可复用同一份数据。
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator

import config
from agent.react import build_react_graph, initial_messages
from api.sessions import SessionStore
from app_context import AppContext, LLMNotConfiguredError, get_context
from data.sources.mysql_source import build_mysql_engine
from logging_setup import get_logger, setup_logging
from mcp_server.server import build_mcp_server

logger = get_logger("api.server")
setup_logging()  # 服务入口：初始化根日志（幂等，重复调用无副作用）


# ── 内置 MCP 端点（P0-1）─────────────────────────────────────────────
# 为什么要挂在同一个进程里：DuckDB 的文件锁是"进程级独占"的——web 和
# mcp 两个进程指向同一个 analysis.duckdb 时，第二个进程连只读都打不开。
# 挂载后只有一个进程、一个 DataService，宿主用 HTTP 连 /mcp 即可。
_mcp = build_mcp_server()
_mcp.settings.streamable_http_path = "/"       # 挂到 /mcp 后真实路径才是 /mcp
_mcp.settings.stateless_http = True            # 本地单用户：不需要会话协商
_mcp.settings.transport_security = _mcp.settings.transport_security.model_copy(
    update={"enable_dns_rebinding_protection": True,
            "allowed_hosts": list(config.MCP_ALLOWED_HOSTS)}
)


class _McpPathAlias:
    """把 /mcp 改写成 /mcp/，避免 307 重定向。

    MCP 客户端（httpx）默认不跟随重定向，POST 到 /mcp 收到 307 就失败。
    Starlette 的 Mount 对"无尾斜杠"的路径会重定向，所以在路由之前
    把 path 补齐。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 只启动 MCP 的 session manager；不在这里打开 DuckDB——
    # 依赖保持懒加载，进程启动失败不应该是因为"没配 key / 库被占"。
    async with _mcp.session_manager.run():
        yield


app = FastAPI(title="data-agent", lifespan=lifespan)
app.add_middleware(_McpPathAlias)


class _RequestId:
    """给每个请求分配 request_id，并回写响应头。

    用纯 ASGI 而不是 ``@app.middleware("http")``：BaseHTTPMiddleware 会把
    流式响应再包一层，SSE 的"边算边发"容易被它打乱，断线时还会抛
    "No response returned"。这里只在响应头里加一个字段，不碰 body。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        rid = ""
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                rid = value.decode("latin-1")[:64]
                break
        rid = rid or uuid.uuid4().hex[:12]
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)


app.add_middleware(_RequestId)


def _error_body(code: str, message: str, request: Request | None = None) -> dict:
    rid = getattr(getattr(request, "state", None), "request_id", "") if request else ""
    # detail 保留：旧前端和既有测试按 detail 读；error 是给新调用方的结构化形态
    return {
        "error": {"code": code, "message": message, "request_id": rid},
        "detail": message,
    }


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    message = (f"{loc}: {first.get('msg', '参数不合法')}" if loc
               else str(first.get("msg", "参数不合法")))
    return JSONResponse(status_code=422,
                        content=_error_body("invalid_request", message, request))


@app.exception_handler(HTTPException)
async def _on_http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(f"http_{exc.status_code}", str(exc.detail), request),
    )


@app.get("/healthz")
def healthz():
    """存活探针：不碰 DuckDB、不碰 LLM，纯内存返回。"""
    return {"status": "ok", "service": "data-agent", "time": time.time()}


app.mount("/mcp", _mcp.streamable_http_app())

# 会话历史（有状态后端）：session_id -> [{role, content}, ...]
_sessions = SessionStore()

# HITL 检查点（Sprint 1）：中断前的图状态存在这里，/api/approve 靠它续跑。
# 每轮一个 thread_id，正常结束会 delete_thread，避免内存随对话无限增长。
_checkpointer = MemorySaver()

# 待确认请求：thread_id -> {session_id, message, payload, created_at}
# 进程内内存态：重启即丢（未完成的审批需要重新提问）。持久化留待后续。
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

# 上传分块大小：1MB 一块，内存占用与文件大小无关
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class ChatRequest(BaseModel):
    # 约束这些字段不是为了"好看"：session_id 是内存字典的键，不限制长度和字符，
    # 一个客户端就能把内存撑大；message 无上限则会直接进 prompt（成本失控）。
    session_id: str = Field(default="default", min_length=1, max_length=64,
                            pattern=r"^[A-Za-z0-9_.-]+$")
    message: str = Field(min_length=1, max_length=4000)
    retry: bool = False  # 断线重连重发标记：后端据此对历史做幂等去重


class MySQLConnectRequest(BaseModel):
    """MySQL 连接请求：url 是完整连接串（含账号密码）。"""
    url: str = Field(min_length=10, max_length=500)
    # 冷热分流行数阈值；None 时用 config.LAZY_LOAD_ROW_LIMIT
    lazy_limit: int | None = None

    @field_validator("url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        # 只接受 MySQL 连接串：这个端点会让服务去连外部数据库，
        # 不做前缀校验等于把"连哪都行"开放给任何调用方。
        if not value.startswith(("mysql://", "mysql+pymysql://", "mariadb://")):
            raise ValueError("仅支持 mysql:// 或 mysql+pymysql:// 连接串")
        return value


class ApproveRequest(BaseModel):
    """HITL 审批：恢复被 interrupt 挂起的那一轮。"""
    thread_id: str = Field(min_length=1, max_length=64)
    decision: str = Field(default="approve", pattern=r"^(approve|reject)$")
    note: str = Field(default="", max_length=500)


def _mask_url(url: str) -> str:
    """日志脱敏：连接串里有密码，日志只留 host/db，密码永远不进日志。"""
    return re.sub(r"//([^:@/]+):[^@]+@", r"//\1:***@", url)


def _sse(event: dict) -> str:
    """把一个事件对象编码成一段 SSE 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _error_stream(message: str) -> Iterator[str]:
    """统一的"流内错误"收尾：答案 + done，避免 500 断流让前端只知道失败。"""
    yield _sse({"type": "answer", "text": message})
    yield _sse({"type": "done"})


@app.post("/api/connect_mysql")
def connect_mysql(req: MySQLConnectRequest):
    """接入远端 MySQL（M5a）：小表搬进 DuckDB、大表留远端下推。

    注意这里是 ``def`` 而不是 ``async def``：连接远端库可能要几秒到几十秒，
    在 ``async def`` 里同步调用会把事件循环卡住——期间所有请求（包括
    别人的聊天流）都停住。同步端点由 Starlette 丢进线程池，不挡事件循环。

    连接是"按需手动"而非服务启动时自动——连接串含账号密码，
    属于用户输入而非固定配置，不该硬编码在启动参数里。
    接入后 service.list_tables() 会带上远端表，Agent 的
    query_sql 工具即可自动路由（冷表下推 / 热表本地）。
    """
    t0 = time.monotonic()
    logger.info("connect_mysql start url=%s", _mask_url(req.url))
    service = get_context().ensure_service()
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


# ── HITL：待确认请求（Sprint 1）────────────────────────────────────

def _prune_pending(now: float | None = None) -> None:
    """清理过期的待确认记录（必须在 _pending_lock 内调用）。"""
    now = time.time() if now is None else now
    for tid in [
        tid for tid, rec in _pending.items()
        if now - rec["created_at"] > config.HITL_PENDING_TTL_SECONDS
    ]:
        _pending.pop(tid, None)


def _register_pending(
    thread_id: str, session_id: str, message: str, payload: dict
) -> None:
    """登记一次待确认：/api/approve 靠 thread_id 找回这一轮。"""
    with _pending_lock:
        _prune_pending()
        _pending[thread_id] = {
            "session_id": session_id,
            "message": message,
            "payload": payload,
            "created_at": time.time(),
        }
        # 上限保护：淘汰最旧的（本地工具不需要排队，只要不无限增长）
        while len(_pending) > config.HITL_MAX_PENDING:
            oldest = min(_pending, key=lambda k: _pending[k]["created_at"])
            _pending.pop(oldest, None)


def _take_pending(thread_id: str) -> dict | None:
    """取出并删除：同一次审批只允许消费一次（重复点击不会执行两次）。"""
    with _pending_lock:
        _prune_pending()
        return _pending.pop(thread_id, None)


def _pending_for_session(session_id: str) -> dict | None:
    """断线重连：同一会话已有待确认的，就回放卡片而不是新开一轮。"""
    with _pending_lock:
        _prune_pending()
        for rec in _pending.values():
            if rec["session_id"] == session_id:
                return rec
    return None


def _interrupt_payload(thread_id: str, interrupts) -> dict:
    """把 LangGraph 的 __interrupt__ 转成前端事件体。

    实测形状（langgraph 1.2.15）：
        {"__interrupt__": (Interrupt(value={...}, id="..."),)}
    它**不是** {节点名: dict}——直接 next(iter(chunk.items())) 会把
    "__interrupt__" 当节点名，后面的 update.get() 直接炸。
    """
    intr = interrupts[0] if isinstance(interrupts, (tuple, list)) else interrupts
    value = getattr(intr, "value", None) or {}
    payload = {
        "thread_id": thread_id,
        "interrupt_id": getattr(intr, "id", None),
    }
    payload.update(value if isinstance(value, dict) else {"detail": str(value)})
    return payload


def _drop_thread(thread_id: str) -> None:
    """一轮结束后删掉检查点：否则内存随对话轮数无限增长。"""
    if not config.HITL_ENABLED:
        return
    try:
        _checkpointer.delete_thread(thread_id)
    except Exception:  # pragma: no cover - 清理失败不影响业务
        logger.debug("checkpoint cleanup failed thread=%s", thread_id)


def _emit_turn(
    *,
    graph,
    graph_input,
    cfg: dict,
    session_id: str,
    message: str,
    history: list[dict],
    t0: float,
) -> Iterator[str]:
    """跑一轮图并把状态增量翻译成 SSE 事件（/api/chat 与 /api/approve 共用）。

    三个容易写错的点：
      1. 中断不是异常，而是流里的一个特殊 chunk（"__interrupt__"）；
      2. charts / tool_events 换成 reducer 之后，节点 update 里是**增量**
         而不是全量，旧实现那套"已发送数量差分"必须删掉，否则第二次
         出现的图表会被误判成重复而丢弃；
      3. 中断时不能写会话历史——这一轮还没有答案，等恢复后再写。
    """
    thread_id = cfg["configurable"]["thread_id"]
    final_state: dict = {}
    try:
        for chunk in graph.stream(graph_input, config=cfg, stream_mode="updates"):
            if "__interrupt__" in chunk:
                payload = _interrupt_payload(thread_id, chunk["__interrupt__"])
                _register_pending(thread_id, session_id, message, payload)
                logger.info("hitl awaiting approval session=%s thread=%s",
                            session_id, thread_id)
                yield _sse({"type": "approval_required", **payload})
                yield _sse({"type": "done"})
                return

            node_name, update = next(iter(chunk.items()))
            # 节点返回空 dict 时，updates 模式会吐 {节点名: None}——
            # guard/plan_sql 在"无风险/无查询"分支就是这种情况，
            # 不兜底会在 final_state.update(None) 上炸（已踩坑）
            update = update or {}
            final_state.update(update)

            # 图表产物：base64 data URI，前端直接渲染成图片
            for image in update.get("charts") or []:
                yield _sse({"type": "chart", "image": image})

            # 工具调用明细：前端渲染成"Agent 在做什么"的步骤卡片
            for ev in update.get("tool_events") or []:
                yield _sse({"type": "tool_call", **ev})

            yield _sse({
                "type": "node",
                "node": node_name,
                "message": update.get("step_text") or "",
            })
    except Exception as exc:
        # 外层 LLM 网络故障/超时等：四节点内层有自己的 fail→重试闭环，
        # 外层没有等价语义，这里统一降级成错误答案而非 500 断流
        logger.exception("chat failed session=%s", session_id)
        yield from _error_stream(f"分析服务暂时不可用: {exc}")
        return

    answer = (
        final_state.get("final_answer")
        or final_state.get("last_response", {}).get("content")
        or "（无输出）"
    )

    # 会话历史滚动追加（M5b 的 history 由后端持有）
    new_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]
    _sessions.commit(session_id, new_history)
    _drop_thread(thread_id)

    logger.info("chat done session=%s thread=%s ms=%.1f",
                session_id, thread_id, (time.monotonic() - t0) * 1000)
    yield _sse({"type": "answer", "text": answer})
    yield _sse({"type": "history", "history": new_history})
    yield _sse({"type": "done"})


def _chat_stream(req: ChatRequest) -> Iterator[str]:
    """对话流生成器：逐节点吐进度事件，最后吐答案和完整历史。

    注意：这是**同步**生成器。FastAPI 的 StreamingResponse 会把同步
    生成器丢进线程池迭代（iterate_in_threadpool），所以里面的
    graph.stream() 阻塞不会卡住事件循环；改成 async 生成器反而会让
    同步调用直接占住 worker 的事件循环（P0-3 的根因）。
    """
    t0 = time.monotonic()
    logger.info("chat start session=%s q=%.80s", req.session_id, req.message)
    # 首个 yield 前先吐"开始"事件：让前端立刻建立 reader 并显示
    # "处理中"，否则浏览器要等第一个真实事件才收流（旧项目同款技巧）
    yield _sse({"type": "start", "message": "开始处理…"})

    # 依赖在这里才真正构造（P0-2）：没配 key 只影响这一条请求
    ctx: AppContext = get_context()
    try:
        service = ctx.ensure_service()
        sql_llm = ctx.ensure_sql_llm()
        chat_llm = ctx.ensure_chat_llm()
        catalog = ctx.ensure_catalog()
    except LLMNotConfiguredError as exc:
        logger.warning("chat aborted: %s", exc)
        yield from _error_stream(str(exc))
        return
    except Exception as exc:  # 库被占/依赖坏了：也给人话，而不是 500 断流
        logger.exception("chat bootstrap failed session=%s", req.session_id)
        yield from _error_stream(f"分析服务暂不可用: {exc}")
        return

    # 这一会话还有待确认的查询 → 回放卡片，不新开一轮。
    # 覆盖两种场景：前端断线重连、用户没点确认就又发了一条消息。
    pending = _pending_for_session(req.session_id)
    if pending is not None:
        yield _sse({"type": "approval_required", **pending["payload"]})
        yield _sse({"type": "done"})
        return

    # 同一会话串行：并发两问会让"读历史→写历史"互相覆盖（P0-3）
    with _sessions.lock(req.session_id):
        history = _sessions.history(req.session_id)
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

        # 每轮一个 thread：检查点只服务"这一轮的审批恢复"，
        # 不承担跨轮对话记忆（记忆仍由 SessionStore 管，避免双写）
        thread_id = f"{req.session_id}:{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        graph = build_react_graph(
            service, sql_llm, chat_llm, catalog.registry, catalog.skills,
            checkpointer=_checkpointer,
            hitl=config.HITL_ENABLED,
        )
        yield from _emit_turn(
            graph=graph,
            graph_input={
                "messages": initial_messages(req.message, history, catalog.skill_catalog),
                "steps": 0,
            },
            cfg=cfg,
            session_id=req.session_id,
            message=req.message,
            history=history,
            t0=t0,
        )


_SSE_HEADERS = {
    "Cache-Control": "no-cache",       # 强制浏览器不缓冲
    "X-Accel-Buffering": "no",         # 强制 Nginx 不缓冲（旧项目踩过的坑）
}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """SSE 流式对话端点（同步 def：FastAPI 会丢线程池，不阻塞事件循环）。"""
    return StreamingResponse(
        _chat_stream(req), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@app.post("/api/approve")
def approve(req: ApproveRequest):
    """HITL 审批端点：批准/拒绝后从断点继续跑同一张图。

    返回的同样是一条 SSE 流（与 /api/chat 同构）：前端拿到后继续消费，
    体验上"确认"只是让对话继续，而不是跳到另一个页面。
    """
    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision 只能是 approve 或 reject")
    rec = _take_pending(req.thread_id)
    if rec is None:
        raise HTTPException(
            status_code=409,
            detail="没有待确认的请求（可能已处理或已过期），请重新提问。",
        )

    ctx = get_context()
    try:
        service = ctx.ensure_service()
        sql_llm = ctx.ensure_sql_llm()
        chat_llm = ctx.ensure_chat_llm()
        catalog = ctx.ensure_catalog()
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    graph = build_react_graph(
        service, sql_llm, chat_llm, catalog.registry, catalog.skills,
        checkpointer=_checkpointer,
        hitl=config.HITL_ENABLED,
    )
    cfg = {"configurable": {"thread_id": req.thread_id}}
    decision = {"approve": req.decision == "approve", "note": req.note}

    def gen() -> Iterator[str]:
        yield _sse({"type": "start", "message": "正在按确认结果继续…"})
        with _sessions.lock(rec["session_id"]):
            yield from _emit_turn(
                graph=graph,
                graph_input=Command(resume=decision),
                cfg=cfg,
                session_id=rec["session_id"],
                message=rec["message"],
                history=_sessions.history(rec["session_id"]),
                t0=time.monotonic(),
            )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    """上传数据文件（CSV/Excel/JSON/Parquet）注册进引擎。

    P0-4：按块读取 + 边读边计数，超过 config.MAX_UPLOAD_BYTES 立刻
    413 中止。旧实现是 ``await file.read()`` 一次读进内存，50MB 的
    限制在 loaders 里才检查——文件早就进内存了，等于没有限制。

    这里是 ``def``：解析 Excel/Parquet 是同步 CPU/IO 操作，放 ``async def``
    里会把事件循环卡住（50MB 文件解析几秒，全站跟着卡几秒）。
    """
    t0 = time.monotonic()
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
    if suffix not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {suffix or '（无扩展名）'}"
                   f"（支持 {sorted(config.ALLOWED_EXTENSIONS)}）",
        )
    # 声明大小能提前拒绝的就不读盘（浏览器/httpx 都会带 Content-Length）
    declared = getattr(file, "size", None)
    if declared is not None and declared > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小限制 {limit_mb}MB",
        )

    logger.info("upload start file=%s", filename)
    tmp_path: Path | None = None
    total = 0
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = file.file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小限制 {limit_mb}MB",
                    )
                tmp.write(chunk)
        # source_name 用用户上传的原始文件名（去扩展名），
        # 不能用临时文件名——那会注册成 tmpm350uj0 这种表名
        source = Path(filename).stem
        service = get_context().ensure_service()
        tables = service.load_file(str(tmp_path), source_name=source)
    except HTTPException:
        raise
    except Exception as exc:  # 加载失败：报 400 而不是 500 内部错误
        logger.warning("upload failed file=%s error=%s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)  # 数据已进引擎，临时文件立即清理
    logger.info("upload ok file=%s bytes=%d tables=%s ms=%.1f",
                filename, total, tables, (time.monotonic() - t0) * 1000)
    return {"tables": tables}


@app.get("/api/tables")
async def tables():
    """当前可用表清单（前端状态栏显示用）。不需要 LLM，也不碰 key。"""
    return {"tables": get_context().ensure_service().list_tables()}


@app.get("/api/mcp/tools")
async def mcp_tools():
    """列出本进程**实际注册**的 MCP 工具。

    存在的理由：宿主（WorkBuddy 等）在连接时缓存工具清单，改完代码不重启就
    只能看到旧工具，而"面板里没有 build_report"很难判断是没重启还是没配对。
    打开这个地址就能一眼看清：返回里有 `build_report` = 新代码已生效。
    """
    tools = await _mcp.list_tools()
    names = sorted(t.name for t in tools)
    return {"count": len(names), "tools": names}


# 静态页面挂载必须放在所有 API 路由之后（mount("/") 只接未被路由匹配的路径）
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
