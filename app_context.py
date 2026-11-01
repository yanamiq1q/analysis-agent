"""运行时装配（P0-2 / P0-6）：懒加载 Service/LLM + 全项目唯一的一份指标注册表。

为什么单独抽一个模块
--------------------
1. **两个入口必须装配同一套对象**。M9 之前 FastAPI 与 MCP 各写一份
   ``service = DataService()`` / ``_metric_registry = ...``，于是出现两个
   真实问题（P0-6）：
     - MCP 的 ``save_metric_definition`` 写进 ``user_metrics.md`` 后，
       Web 端从来不读这个文件 → 用户保存的口径 Web 里就是不认；
     - 两个进程各自打开同一个 DuckDB 文件 → 第二个进程直接被文件锁拒绝。
   装配只写一处，"两个入口看到的世界"才可能一致。

2. **装配必须延迟到"第一次真正要用"**（P0-2）。OpenAI 客户端在构造时
   就校验 api_key，没有 ``DEEPSEEK_API_KEY`` 会直接抛 OpenAIError。原来
   在 ``api/server.py`` 的模块顶层构造，导致：
     - 没配 key 时服务进程根本起不来（连静态页、上传、/api/tables 都不可用）；
     - ``pytest`` 在 collection 阶段就 ImportError/OpenAIError，249 个
       测试一个都跑不了（CI 必红）。
   现在的语义：import 不碰网络、不碰 DuckDB；真正调用时才构造，且只有
   需要 LLM 的请求会失败，其余能力照常可用。

3. **注册表可重载**。用户级口径落盘后要"本次运行内立刻生效"，
   MetricCatalog.reload() 是唯一的重载入口，Web/MCP 都走它。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config
from agent.llm import ReActChatModel, SqlGenerator, build_deepseek_chat_generator, build_deepseek_generator
from agent.skill_loader import (
    Skill,
    SkillDiagnostic,
    build_metric_registry,
    list_skill_catalog,
    load_user_metrics,
    parse_skill_dir,
    save_user_metric,
)
from agent.tools import TOOL_SCHEMAS
from data.metrics import Metric
from data.service import DataService
from logging_setup import get_logger

logger = get_logger("app.context")

# 用户级指标落盘位置（MCP save_metric_definition 写，Web/MCP 都读）
USER_METRICS_PATH = config.DATA_DIR / "user_metrics.md"


class LLMNotConfiguredError(RuntimeError):
    """缺 DEEPSEEK_API_KEY：把 SDK 的英文报错换成人话，便于前端展示。"""


class MetricCatalog:
    """指标口径注册表 = 项目级技能包（skills/） + 用户级文件（user_metrics.md）。

    唯一实例由 AppContext 持有；两个入口共用，写入后统一 reload，
    不再出现"MCP 存的口径 Web 不认"这种各读各的情况（P0-6）。
    """

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        user_metrics_path: Path | str | None = None,
    ) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else config.PROJECT_ROOT / "skills"
        self.user_metrics_path = (
            Path(user_metrics_path) if user_metrics_path else USER_METRICS_PATH
        )
        self.skills: dict[str, Skill] = {}
        self.registry: list[Metric] = []
        self.diagnostics: list[SkillDiagnostic] = []
        # reload 可能被并发请求/工具调用同时触发（保存指标的瞬间），
        # 用锁保证"skills + user + registry"三者永远是一致快照
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> "MetricCatalog":
        """重新解析技能包与用户级文件，原子替换三个字段。"""
        with self._lock:
            skills, diagnostics = parse_skill_dir(self.skills_dir)
            user_metrics, user_diag = load_user_metrics(self.user_metrics_path)
            if user_diag is not None:
                diagnostics = [*diagnostics, user_diag]
            self.skills = skills
            self.diagnostics = diagnostics
            self.registry = build_metric_registry(skills) + user_metrics
            return self

    @property
    def skill_catalog(self) -> str:
        """system prompt 里的技能目录段（渐进式披露的"目录常驻"部分）。"""
        return list_skill_catalog(self.skills)

    def save_user_metric(self, metric: Metric) -> None:
        """写盘 + 立刻重载：本次运行内后续请求即可命中新口径。"""
        save_user_metric(self.user_metrics_path, metric)
        self.reload()


@dataclass
class AppContext:
    """一次进程内的运行时依赖集合（全部可注入，测试不必碰真实 key/磁盘）。

    字段默认 None：交给 ensure_*() 懒构造。测试直接赋值即可覆盖，
    不需要 monkeypatch 模块级变量这种易漂移的做法。
    """

    service: Optional[DataService] = None
    sql_llm: Optional[SqlGenerator] = None
    chat_llm: Optional[ReActChatModel] = None
    catalog: Optional[MetricCatalog] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 懒构造 ────────────────────────────────────────────────────

    def ensure_service(self) -> DataService:
        with self._lock:
            if self.service is None:
                self.service = DataService()
            return self.service

    def ensure_catalog(self) -> MetricCatalog:
        with self._lock:
            if self.catalog is None:
                self.catalog = MetricCatalog()
                for diag in self.catalog.diagnostics:
                    logger.warning("skill load failed path=%s error=%s",
                                   diag.path, diag.error)
            return self.catalog

    def ensure_sql_llm(self) -> SqlGenerator:
        """内层 SQL 生成器。缺 key 时抛人话错误（不再让 import 阶段炸）。"""
        with self._lock:
            if self.sql_llm is None:
                self.sql_llm = _build_or_hint(build_deepseek_generator)
            return self.sql_llm

    def ensure_chat_llm(self) -> ReActChatModel:
        """外层 ReAct 调度模型（带 tools）。"""
        with self._lock:
            if self.chat_llm is None:
                self.chat_llm = _build_or_hint(
                    lambda: build_deepseek_chat_generator(TOOL_SCHEMAS)
                )
            return self.chat_llm


def _build_or_hint(factory):
    """构造 LLM 客户端，把"没配 key"翻译成人话。"""
    try:
        return factory()
    except Exception as exc:  # pragma: no cover - 依赖 openai SDK 的具体报错
        if "api_key" in str(exc).lower() or "credentials" in str(exc).lower():
            raise LLMNotConfiguredError(
                "未配置 DEEPSEEK_API_KEY：请设置环境变量后重启服务"
                "（纯数据浏览/上传不受影响，只有自然语言分析需要 LLM）。"
            ) from exc
        raise


# ── 进程级单例 ────────────────────────────────────────────────────

_context: Optional[AppContext] = None
_context_lock = threading.Lock()


def get_context() -> AppContext:
    """拿进程级上下文（首次调用才创建，本身不碰磁盘/网络）。"""
    global _context
    with _context_lock:
        if _context is None:
            _context = AppContext()
        return _context


def set_context(ctx: Optional[AppContext]) -> None:
    """替换进程级上下文（测试/嵌入式宿主用）。"""
    global _context
    with _context_lock:
        _context = ctx
