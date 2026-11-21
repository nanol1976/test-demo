"""台账仓储实现。

铁律：只允许追加与状态更新，任何删除/清空/批量覆盖都不提供。
"""
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PipelineLedger, TaskLedger

PIPELINE_STATUSES = {"pending", "running", "success", "aborted", "failed"}
TASK_STATUSES = {
    "待拆解", "待调度下发", "执行中", "待验收", "验收通过", "验收驳回", "执行失败",
}

# 排序白名单（Batch8）：只允许按时间/编号排，不允许前端传 SQL 片段
PIPELINE_SORTS = {"start_time_desc", "start_time_asc"}
TASK_SORTS = {"task_id_asc", "task_id_desc"}


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

    # ---- 只读查询（Batch3：分页 + 筛选；Batch8：关键字 + 排序）----
    @staticmethod
    def _pipeline_filters(status, start_from, start_to, q):
        conds = []
        if status:
            if status not in PIPELINE_STATUSES:
                raise ValueError(f"非法流水线状态: {status}")
            conds.append(PipelineLedger.overall_status == status)
        if start_from is not None:
            conds.append(PipelineLedger.start_time >= start_from)
        if start_to is not None:
            conds.append(PipelineLedger.start_time <= start_to)
        if q:
            conds.append(
                or_(
                    PipelineLedger.trace_id.contains(q),
                    PipelineLedger.original_demand.contains(q),
                )
            )
        return conds

    def list_pipelines(
        self,
        status: Optional[str] = None,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
        q: Optional[str] = None,
        sort: str = "start_time_desc",
        limit: int = 20,
        offset: int = 0,
    ) -> List[PipelineLedger]:
        if sort not in PIPELINE_SORTS:
            raise ValueError(f"非法排序: {sort}")
        stmt = select(PipelineLedger)
        conds = self._pipeline_filters(status, start_from, start_to, q)
        if conds:
            stmt = stmt.where(*conds)
        order = (
            PipelineLedger.start_time.asc()
            if sort == "start_time_asc"
            else PipelineLedger.start_time.desc()
        )
        stmt = stmt.order_by(order).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count_pipelines(
        self,
        status: Optional[str] = None,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
        q: Optional[str] = None,
    ) -> int:
        # Batch8：改用 SQL count()，不再把全表拉回来数（台账长大后的真实开销）
        stmt = select(func.count()).select_from(PipelineLedger)
        conds = self._pipeline_filters(status, start_from, start_to, q)
        if conds:
            stmt = stmt.where(*conds)
        return int(self.db.execute(stmt).scalar() or 0)

    @staticmethod
    def _task_filters(trace_id, status, target_worker, q, instance_id=None, include_all=False):
        conds = []
        if trace_id:
            conds.append(TaskLedger.trace_id == trace_id)
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"非法任务状态: {status}")
            conds.append(TaskLedger.status == status)
        if target_worker:
            conds.append(TaskLedger.target_worker == target_worker)
        if q:
            conds.append(TaskLedger.task_id.contains(q))
        if instance_id:
            # Batch31-D：按「谁在问」收敛。指定了岗位的任务只给该岗位看；
            # 未指定岗位的老任务保持先到先得，不对任何人隐藏。
            conds.append(
                or_(TaskLedger.target_instance.is_(None), TaskLedger.target_instance == instance_id)
            )
        elif not include_all:
            # 没说自己是哪个岗位的请求（含未升级的老 Worker），一律看不到「指定岗位」的任务。
            # 否则老 Worker 不带实例名也能把指定给别人的活抢走，分工就是空话。
            conds.append(TaskLedger.target_instance.is_(None))
        return conds

    def search_tasks(
        self,
        trace_id: Optional[str] = None,
        status: Optional[str] = None,
        target_worker: Optional[str] = None,
        q: Optional[str] = None,
        sort: str = "task_id_asc",
        limit: int = 20,
        offset: int = 0,
        instance_id: Optional[str] = None,
        include_all: bool = False,
    ) -> List[TaskLedger]:
        if sort not in TASK_SORTS:
            raise ValueError(f"非法排序: {sort}")
        stmt = select(TaskLedger)
        conds = self._task_filters(trace_id, status, target_worker, q, instance_id, include_all)
        if conds:
            stmt = stmt.where(*conds)
        order = TaskLedger.task_id.desc() if sort == "task_id_desc" else TaskLedger.task_id.asc()
        stmt = stmt.order_by(order).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count_tasks(
        self,
        trace_id: Optional[str] = None,
        status: Optional[str] = None,
        target_worker: Optional[str] = None,
        q: Optional[str] = None,
        instance_id: Optional[str] = None,
        include_all: bool = False,
    ) -> int:
        stmt = select(func.count()).select_from(TaskLedger)
        conds = self._task_filters(trace_id, status, target_worker, q, instance_id, include_all)
        if conds:
            stmt = stmt.where(*conds)
        return int(self.db.execute(stmt).scalar() or 0)

    # ---- Batch4：超时/心跳/熔断/暂停所需的读取与状态位 ----
    def worker_registered(self, worker_id: str, instance_id: str) -> bool:
        """Batch31-D：这个岗位是否在注册表里出现过（按 (worker_id, instance_id) 主键）。"""
        from app.models import WorkerRegistry

        return self.db.get(WorkerRegistry, (worker_id, instance_id)) is not None

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
        target_instance: Optional[str] = None,
        review_of: Optional[str] = None,
    ) -> TaskLedger:
        if status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        row = TaskLedger(
            task_id=task_id,
            trace_id=trace_id,
            target_worker=target_worker,
            target_instance=target_instance,
            status=status,
            max_retry=max_retry,
            remaining_retry=max_retry,
            is_high_risk=is_high_risk,
            depends_on=list(depends_on or []),
            review_of=review_of,
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

    def accept_pending_tasks(self, trace_id: str, force_accept: bool = False) -> List[str]:
        """把一条流水线下的「待验收」任务推到「验收通过」，返回被推进的 task_id。

        Batch31：协议里 7 种报文没有一种能写「验收通过」—— 任务回传后永远停在「待验收」，
        六步 SOP 的第 5 步（验收）实际是空的。这里只做状态推进（台账铁律允许），
        不新增报文类型，由既有的 task_summary 在 Master 显式声明 accept_tasks 时调用。

        Batch32-1 起加一道闸门：只推 `review_verdict ∈ {pass, None}` 的任务。
        - `fail` 的单在驳回时就已离开「待验收」，正常走不到这里（留着这行是双保险）；
        - `unreviewed`（没找到合格复核者）的單必须显式 `force_accept=True` 才推，
          否则等于把"没人审过"的活混进"审过了"里。
        """
        rows = self.db.execute(
            select(TaskLedger).where(
                TaskLedger.trace_id == trace_id, TaskLedger.status == "待验收"
            )
        ).scalars().all()
        accepted = []
        for row in rows:
            verdict = getattr(row, "review_verdict", None)
            if verdict == "fail":
                continue
            if verdict == "unreviewed" and not force_accept:
                continue
            row.status = "验收通过"
            accepted.append(row.task_id)
        if accepted:
            self.db.commit()
        return accepted

    def mark_dispatch_time(self, task_id: str, when: datetime) -> Optional[TaskLedger]:
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.dispatch_time = when
        self.db.commit()
        self.db.refresh(row)
        return row

    def record_dispatch(
        self,
        task_id: str,
        when: datetime,
        payload_json: Optional[str] = None,
        target_worker: Optional[str] = None,
        target_instance: Optional[str] = None,
    ) -> Optional[TaskLedger]:
        """派发落账：dispatch_time + 载荷原文（+ 必要时纠正执行方归属）。

        只更新、不删除。重派时以最新一次派发为准（覆盖 payload 与 dispatch_time），
        因为 Worker 要按"这一次"的规格执行。
        """
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.dispatch_time = when
        if payload_json is not None:
            row.dispatch_payload = payload_json
        if target_worker is not None:
            row.target_worker = target_worker
        # Batch31-D：重派时以最新一次指定为准；显式传空串表示改回"不指定岗位"
        if target_instance is not None:
            row.target_instance = target_instance or None
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_heartbeat(self, task_id: str, when: datetime) -> Optional[TaskLedger]:
        """记最近一次心跳时间（不改状态）。只更新，不删除。"""
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return None
        row.last_heartbeat_time = when
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

    def mark_done_instance(self, task_id: str, instance_id: str) -> Optional[TaskLedger]:
        """Batch31-D：把「实际由哪个岗位完成」写进台账。

        回执报文给出 metadata.instance_id 时调用；老 Worker 不给就保持空，
        不阻断回执（兼容优先），但台账上会显示为空，无法追溯。
        """
        row = self.db.get(TaskLedger, task_id)
        if row is None or not instance_id:
            return None
        row.done_instance = instance_id[:64]
        self.db.commit()
        self.db.refresh(row)
        return row

    # ---- Batch32-1：异构复核 ----
    def pick_reviewer(self, exclude_instance: Optional[str]) -> Optional[tuple]:
        """挑复核者：在线、不是执行者本人、当前在跑的活最少。

        排序口径写死（防"随便挑一个在线就行"）：
          1) 排除 `exclude_instance`（执行者实例）——复核不能自己审自己；
          2) 只算 `status=running` 且最近心跳在 `heartbeat_timeout_seconds` 内的；
          3) 按 `in_flight` 升序，忙闲相同时取 `last_seen` 最新的（刚报过心跳的更可信）。
        返回 (worker_id, instance_id)；一个都不合格返回 None（调用方标 unreviewed）。
        """
        from app.core.clock import ensure_utc
        from app.core.config import get_settings
        from app.models import WorkerRegistry

        timeout = int(get_settings().heartbeat_timeout_seconds)
        now = datetime.now(timezone.utc)
        rows = self.db.execute(select(WorkerRegistry)).scalars().all()
        cands = []
        for row in rows:
            if exclude_instance and row.instance_id == exclude_instance:
                continue
            if row.status != "running":
                continue
            age = (now - ensure_utc(row.last_seen)).total_seconds()
            if age > timeout:
                continue
            cands.append(row)
        if not cands:
            return None
        best = sorted(
            cands,
            key=lambda r: (int(r.in_flight), -ensure_utc(r.last_seen).timestamp()),
        )[0]
        return best.worker_id, best.instance_id

    def set_review_verdict(
        self, task_id: str, verdict: str, items_json: Optional[str] = None
    ) -> bool:
        """把复核结论写进被复核的任务：只写一次。

        返回 True = 本次写成功；False = 任务不存在，或已有结论（不得改写）。
        「只写一次」是防作弊的第 4 条：复核结论一旦落账，任何后续回执都不许覆盖。
        """
        if verdict not in {"pass", "fail", "unreviewed"}:
            raise ValueError(f"非法复核结论: {verdict}")
        row = self.db.get(TaskLedger, task_id)
        if row is None:
            return False
        if getattr(row, "review_verdict", None):
            return False
        row.review_verdict = verdict
        if items_json is not None:
            row.review_items = items_json
        self.db.commit()
        self.db.refresh(row)
        return True

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
