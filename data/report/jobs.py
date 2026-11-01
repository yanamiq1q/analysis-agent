"""报告任务层（PR2）：MCP 工具背后的 job 管理。

为什么需要它
------------
MCP 的工具调用是**一次请求-响应**，宿主有超时（通常几十秒），而一份报告
要查十几条 SQL、画几张图。如果 `build_report` 同步跑完再返回，宿主会先超时，
用户看到的是"工具调用失败"，但报告其实还在后台跑——最糟的一种不一致。

所以采用「提交 + 轮询」：

    build_report(period, wait_seconds=20) → 最多等 20 秒
        ├─ 等到了：直接返回 succeeded + 产物路径
        └─ 没等到：返回 running + job_id，提示宿主用 get_report 轮询

三个约束：
  1. **状态在文件里**（status.json），不在内存里——进程重启后 get_report
     仍然能回答"这个 job 到底怎么了"；
  2. **幂等**：同一 period + template + focus 生成同一个 job_id，重复提交
     不会跑两遍；
  3. **同一 job 只跑一次**：_jobs 里已有记录就不再提交线程。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
from data.report.pipeline import (
    ReportError,
    ReportRequest,
    make_job_id,
    run_report,
    write_status,
)
from logging_setup import get_logger

logger = get_logger("data.report.jobs")

_TERMINAL = {"succeeded", "failed"}
_POLL_INTERVAL_SECONDS = 0.3


@dataclass(frozen=True)
class JobHandle:
    """内存里只放"我提交过它"这件事；真正的状态以 status.json 为准。"""

    job_id: str
    submitted_at: float


class JobManager:
    def __init__(self, max_workers: int = 2, root: Path | str | None = None) -> None:
        self._root = Path(root) if root else None
        self._jobs: dict[str, JobHandle] = {}
        self._lock = threading.Lock()
        self._pool = _Pool(max_workers)

    # ── 基础 ────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root or Path(config.REPORT_JOBS_DIR)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    # ── 提交 ────────────────────────────────────────────────────

    def submit(self, service, request: ReportRequest) -> str:
        """提交一次报告任务，立刻返回 job_id（不阻塞）。

        提交瞬间就写一条 ``queued`` 状态：否则宿主在 ``wait_seconds=0`` 时
        拿到的会是 "unknown"（目录还没被工作线程创建），前端会显示"任务不存在"。
        """
        job_id = make_job_id(request, request.period_start, request.period_end)
        with self._lock:
            if job_id in self._jobs:
                return job_id
            self._jobs[job_id] = JobHandle(job_id, time.time())

            job_dir = self.job_dir(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            prior = _read_status(job_dir / "status.json")
            # 已成功的任务且没要求重跑：不要用 queued 覆盖它（否则
            # "重启后重新提交同一周期"会把已完成状态刷成排队中）
            if not (prior.get("status") == "succeeded" and not request.force):
                write_status(
                    job_dir,
                    job_id=job_id,
                    status="queued",
                    stage="queued",
                    progress=0.0,
                    started_at=_now_iso(),
                )
            self._pool.submit(self._run, service, request, job_id)
            return job_id

    def _run(self, service, request: ReportRequest, job_id: str) -> None:
        try:
            run_report(
                service,
                request,
                out_root=self.root,
                job_id=job_id,
            )
        except Exception as exc:  # run_report 正常会先写 failed 再抛；这里兜底
            logger.warning("report job failed id=%s error=%s", job_id, exc)
            status = self.job_dir(job_id) / "status.json"
            if not status.is_file():
                self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
                write_status(
                    self.job_dir(job_id),
                    job_id=job_id,
                    status="failed",
                    stage="failed",
                    error={"code": type(exc).__name__, "message": str(exc)},
                )

    # ── 查询 ────────────────────────────────────────────────────

    def wait(self, job_id: str, timeout_seconds: float) -> dict:
        """等到终态或超时（返回最后一次状态，不抛异常）。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            payload = self.payload(job_id) or _unknown(job_id)
            if payload.get("status") in _TERMINAL or time.monotonic() >= deadline:
                return payload
            time.sleep(_POLL_INTERVAL_SECONDS)

    def payload(self, job_id: str) -> Optional[dict]:
        """读 status.json 组装成给宿主的响应；目录不存在返回 None。"""
        job_dir = self.job_dir(job_id)
        if not job_dir.is_dir():
            return None

        status: dict[str, Any] = {}
        status_file = job_dir / "status.json"
        if status_file.is_file():
            try:
                status = json.loads(status_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                status = {"status": "unknown", "error": {"code": "BAD_STATUS_FILE",
                                                         "message": "status.json 解析失败"}}
        else:
            # 提交了但线程还没写第一个状态：给宿主一个明确的"排队中"
            with self._lock:
                queued = job_id in self._jobs
            status = {
                "status": "queued" if queued else "unknown",
                "stage": "queued",
            }

        status.setdefault("job_id", job_id)
        status["spec_path"] = str(job_dir / "report.json") if (job_dir / "report.json").is_file() else ""
        status["artifacts"] = _artifacts(job_dir)
        status["job_dir"] = str(job_dir)

        if status.get("status") in ("queued", "running"):
            status["hint"] = "任务仍在进行：请每 5 秒调用 get_report(job_id) 直到 status 变为 succeeded/failed"
            if status.get("status") == "running" and _is_stale(status):
                # 进程重启后内存里的线程没了，但 status.json 还停在 running
                status["stale"] = True
                status["hint"] = (
                    "该任务的状态长时间没有更新，可能是服务重启导致中断；"
                    "请用 build_report(..., force=true) 重新提交"
                )
        return status

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)


def _artifacts(job_dir: Path) -> list[str]:
    """产物清单：report.json + assets/*，按路径排序（对方要能直接读）。"""
    items: list[str] = []
    report = job_dir / "report.json"
    if report.is_file():
        items.append(str(report))
    assets = job_dir / "assets"
    if assets.is_dir():
        items.extend(str(p) for p in sorted(assets.iterdir()) if p.is_file())
    return items


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_status(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _is_stale(status: dict) -> bool:
    started = status.get("started_at") or status.get("updated_at")
    if not started:
        return False
    try:
        started_at = datetime.fromisoformat(str(started))
    except ValueError:
        return False
    age = datetime.now(started_at.tzinfo) - started_at
    return age.total_seconds() > config.REPORT_TIMEOUT_SECONDS


def _unknown(job_id: str) -> dict:
    return {"job_id": job_id, "status": "unknown", "stage": "unknown"}


class _Pool:
    """极薄的线程池封装：只为了让"无任务时线程能退出"这件事可控。"""

    def __init__(self, max_workers: int) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="report-job"
        )

    def submit(self, fn, *args) -> None:
        self._executor.submit(fn, *args)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)


# ── 进程级单例 ────────────────────────────────────────────────────

_manager: Optional[JobManager] = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager


def reset_job_manager() -> None:
    """测试/进程重启用：关掉旧池，下一次 get 会新建。"""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
