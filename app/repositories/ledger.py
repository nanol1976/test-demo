"""台账仓储实现。

铁律：只允许追加与状态更新，任何删除/清空/批量覆盖都不提供。
"""
from datetime import datetime
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PipelineLedger, TaskLedger

PIPELINE_STATUSES = {"pending", "running", "success", "aborted", "failed"}
TASK_STATUSES = {
    "待拆解", "待调度下发", "执行中", "待验收", "验收通过", "验收驳回", "执行失败",
}


class LedgerRepository:
    """台账访问入口，禁止删除语义。"""

    def __init__(self, db: Session):
        self.db = db

    # ---- 流水线台账 ----
    def create_pipeline(self, trace_id: str, original_demand: str) -> PipelineLedger:
        """get-or-create：并发下同一 trace 只应有一条流水线。"""
        existing = self.db.get(PipelineLedger, trace_id)
        if existing is not None:
            return existing
        row = PipelineLedger(
            trace_id=trace_id,
            original_demand=original_demand,
            overall_status="pending",
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.get(PipelineLedger, trace_id)
            if existing is None:
                raise
            return existing
        self.db.refresh(row)
        return row

    def get_pipeline(self, trace_id: str) -> Optional[PipelineLedger]:
        return self.db.get(PipelineLedger, trace_id)

    def update_pipeline_status(self, trace_id: str, status: str, end_time: Optional[datetime] = None) -> Optional[PipelineLedger]:
        if status not in PIPELINE_STATUSES:
            raise ValueError(f"非法流水线状态: {status}")
        row = self.db.get(PipelineLedger, trace_id)
        if row is None:
            return None
        row.overall_status = status
        if end_time is not None:
            row.end_time = end_time
        self.db.commit()
        self.db.refresh(row)
        return row

    # ---- 只读查询（Batch3：分页 + 筛选，不含任何写操作）----
    def list_pipelines(
        self,
        status: Optional[str] = None,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[PipelineLedger]:
        stmt = select(PipelineLedger)
        if status:
            if status not in PIPELINE_STATUSES:
                raise ValueError(f"非法流水线状态: {status}")
            stmt = stmt.where(PipelineLedger.overall_status == status)
        if start_from is not None:
            stmt = stmt.where(PipelineLedger.start_time >= start_from)
        if start_to is not None:
            stmt = stmt.where(PipelineLedger.start_time <= start_to)
        stmt = stmt.order_by(PipelineLedger.start_time.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count_pipelines(
        self,
        status: Optional[str] = None,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
    ) -> int:
        stmt = select(PipelineLedger)
        if status:
            if status not in PIPELINE_STATUSES:
                raise ValueError(f"非法流水线状态: {status}")
            stmt = stmt.where(PipelineLedger.overall_status == status)
        if start_from is not None:
            stmt = stmt.where(PipelineLedger.start_time >= start_from)
        if start_to is not None:
            stmt = stmt.where(PipelineLedger.start_time <= start_to)
        return len(list(self.db.execute(stmt).scalars().all()))

    def search_tasks(
        self,
        trace_id: Optional[str] = None,
        status: Optional[str] = None,
        target_worker: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[TaskLedger]:
        stmt = select(TaskLedger)
        if trace_id:
            stmt = stmt.where(TaskLedger.trace_id == trace_id)
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"非法任务状态: {status}")
            stmt = stmt.where(TaskLedger.status == status)
        if target_worker:
            stmt = stmt.where(TaskLedger.target_worker == target_worker)
        stmt = stmt.order_by(TaskLedger.task_id).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count_tasks(
        self,
        trace_id: Optional[str] = None,
        status: Optional[str] = None,
        target_worker: Optional[str] = None,
    ) -> int:
        stmt = select(TaskLedger)
        if trace_id:
            stmt = stmt.where(TaskLedger.trace_id == trace_id)
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"非法任务状态: {status}")
            stmt = stmt.where(TaskLedger.status == status)
        if target_worker:
            stmt = stmt.where(TaskLedger.target_worker == target_worker)
        return len(list(self.db.execute(stmt).scalars().all()))

    # ---- Batch4：超时/心跳/熔断/暂停所需的读取与状态位 ----
    def list_tasks_by_status(self, status: str) -> List[TaskLedger]:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        stmt = select(TaskLedger).where(TaskLedger.status == status)
        return list(self.db.execute(stmt).scalars().all())

    def last_heartbeat_at(self, task_id: str):
        from app.models import MessageArchive

        stmt = (
            select(MessageArchive.timestamp)
            .where(
                MessageArchive.task_id == task_id,
                MessageArchive.msg_type == "task_heartbeat",
            )
            .order_by(MessageArchive.timestamp.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def count_tasks_by_status_in_trace(self, trace_id: str, status: str) -> int:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        stmt = select(TaskLedger).where(
            TaskLedger.trace_id == trace_id, TaskLedger.status == status
        )
        return len(list(self.db.execute(stmt).scalars().all()))

    def set_pause(self, trace_id: str, flag: bool) -> Optional[PipelineLedger]:
        row = self.db.get(PipelineLedger, trace_id)
        if row is None:
            return None
        row.pause_flag = bool(flag)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_pipelines_paused(self) -> List[PipelineLedger]:
        stmt = select(PipelineLedger).where(PipelineLedger.pause_flag == True)  # noqa: E712
        return list(self.db.execute(stmt).scalars().all())

    # ---- 子任务台账 ----
    def create_task(
        self,
        trace_id: str,
        task_id: str,
        target_worker: str,
        depends_on: Optional[List[str]] = None,
        max_retry: int = 3,
        is_high_risk: bool = False,
        status: str = "待拆解",
    ) -> TaskLedger:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        row = TaskLedger(
            task_id=task_id,
            trace_id=trace_id,
            target_worker=target_worker,
            status=status,
            max_retry=max_retry,
            remaining_retry=max_retry,
            is_high_risk=is_high_risk,
            depends_on=list(depends_on or []),
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            # 并发重复建同一任务：回退为已存在记录，绝不覆盖
            self.db.rollback()
            existing = self.db.get(TaskLedger, task_id)
            if existing is None:
                raise
            return existing
        self.db.refresh(row)
        return row

    def get_task(self, task_id: str) -> Optional[TaskLedger]:
        return self.db.get(TaskLedger, task_id)

    def list_tasks(self, trace_id: str) -> List[TaskLedger]:
        stmt = select(TaskLedger).where(TaskLedger.trace_id == trace_id)
        return list(self.db.execute(stmt).scalars().all())

    def update_task_status(self, task_id: str, status: str) -> Optional[TaskLedger]:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.status = status
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_dispatch_time(self, task_id: str, when: datetime) -> Optional[TaskLedger]:
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.dispatch_time = when
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_return_time(self, task_id: str, when: datetime) -> Optional[TaskLedger]:
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.return_time = when
        self.db.commit()
        self.db.refresh(row)
        return row

    def record_reject(self, task_id: str, reason: str) -> Optional[TaskLedger]:
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.reject_reason = reason
        row.remaining_retry = max(0, row.remaining_retry - 1)
        self.db.commit()
        self.db.refresh(row)
        return row

    def record_abort(self, task_id: str, reason: str) -> Optional[TaskLedger]:
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.abort_reason = reason
        self.db.commit()
        self.db.refresh(row)
        return row
