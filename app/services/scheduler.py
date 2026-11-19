"""后台判定线程（Batch4）：超时 / 心跳告警 / 熔断。

设计要点：
- `run_once()` 是纯函数式的一轮扫描，可被验收脚本直接调用（配合 FakeClock 完全确定）；
- 后台线程只是按间隔反复调用 `run_once()`，不做任何额外判断；
- 只改状态与原因字段，绝不删除行。
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.clock import ensure_utc, get_clock
from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.ledger import LedgerRepository
from app.services.breaker import breaker

logger = get_logger(__name__)

HEARTBEAT_ALERT_PREFIX = "[心跳告警]"


@dataclass
class SchedulerReport:
    timed_out: List[str] = field(default_factory=list)
    heartbeat_alerted: List[str] = field(default_factory=list)
    tripped: List[str] = field(default_factory=list)


def run_once(db: Session, now: Optional[datetime] = None) -> SchedulerReport:
    """执行一轮判定，返回本轮动作明细。"""
    settings = get_settings()
    clock_now = ensure_utc(now) if now is not None else ensure_utc(get_clock().now())
    repo = LedgerRepository(db)
    report = SchedulerReport()

    running = repo.list_tasks_by_status("执行中")

    # ---- 1. 派发超时：dispatch_time 起算超过阈值 → 判执行失败 ----
    timeout = timedelta(seconds=settings.task_timeout_seconds)
    for task in running:
        dispatched_at = ensure_utc(task.dispatch_time)
        if dispatched_at is None:
            continue
        if clock_now - dispatched_at > timeout:
            repo.record_abort(
                task.task_id,
                f"[超时] 调度后超过 {settings.task_timeout_seconds}s 未回传（判定时刻 {clock_now.isoformat()}）",
            )
            repo.update_task_status(task.task_id, "执行失败")
            report.timed_out.append(task.task_id)
            logger.warning("任务超时判定失败：%s", task.task_id)

    # ---- 2. 心跳告警：只留痕，不改状态（是否杀任务由 Master 决定）----
    hb_timeout = timedelta(seconds=settings.heartbeat_timeout_seconds)
    for task in running:
        if task.task_id in report.timed_out:
            continue
        last = ensure_utc(repo.last_heartbeat_at(task.task_id))
        baseline = last or ensure_utc(task.dispatch_time)
        if baseline is None:
            continue
        if clock_now - baseline <= hb_timeout:
            continue
        if (task.abort_reason or "").startswith(HEARTBEAT_ALERT_PREFIX):
            continue
        repo.record_abort(
            task.task_id,
            f"{HEARTBEAT_ALERT_PREFIX} 超过 {settings.heartbeat_timeout_seconds}s 无心跳"
            f"（最后心跳 {baseline.isoformat()}），仅告警不改状态",
        )
        report.heartbeat_alerted.append(task.task_id)
        logger.warning("心跳告警：%s", task.task_id)

    # ---- 3. 熔断：同一 trace 失败任务数达阈值 → 熔断并置 pipeline failed ----
    traces = {t.trace_id for t in repo.list_tasks_by_status("执行失败")}
    for trace_id in traces:
        if breaker.is_open(trace_id):
            continue
        failed = repo.count_tasks_by_status_in_trace(trace_id, "执行失败")
        if failed >= settings.failure_threshold:
            reason = f"连续失败任务数 {failed} ≥ 阈值 {settings.failure_threshold}"
            if breaker.trip(trace_id, reason):
                repo.update_pipeline_status(trace_id, "failed")
                report.tripped.append(trace_id)
                logger.error("熔断触发：trace=%s %s", trace_id, reason)

    return report


class Scheduler:
    """后台判定线程的宿主。"""

    def __init__(self, session_factory, interval_seconds: Optional[float] = None) -> None:
        self._session_factory = session_factory
        self.interval = (
            interval_seconds
            if interval_seconds is not None
            else get_settings().scan_interval_seconds
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> SchedulerReport:
        db = self._session_factory()
        try:
            return run_once(db)
        finally:
            db.close()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        logger.info("后台判定线程已启动，扫描间隔 %ss", self.interval)

    def _loop(self) -> None:
        while self._running:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 扫描线程不能因单次异常退出
                logger.exception("后台判定轮次失败，继续下一轮")
            time.sleep(self.interval)

    def stop(self, join_timeout: float = 2.0) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None
        logger.info("后台判定线程已停止")
