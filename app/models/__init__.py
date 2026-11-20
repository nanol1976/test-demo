from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Text, DateTime, Boolean, Integer, JSON, ForeignKey, Index, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class WorkerRegistry(Base):
    """执行方注册表（Batch13）。

    解决 Batch12 在线视图的盲区：没有任务的 Worker 在台账里一行都没有，
    看起来像"消失了"。这里让 Worker 无论忙闲都周期上报，平台自己维护在线状态。

    只 upsert + 读，不做删除（台账铁律同样适用于这张表）。
    """

    __tablename__ = "worker_registry"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Batch14：同一 worker_id 可有多台进程（多实例），用 instance_id 区分，
    # 否则两台机器会互相覆盖 last_seen，看板上分不清谁在线。
    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 物理列名用 concurrency_limit：`limit` 是 SQL 关键字，裸写在 DDL 里会语法错误
    # （Batch13 的 v005 就踩到了，只是当时建表被模型抢先做掉才没暴露）
    limit: Mapped[int] = mapped_column("concurrency_limit", Integer, nullable=False, default=1)
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    handled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Batch15：平台下发的期望并发上限。0 = 没设过，Worker 按自己的 --concurrency 跑。
    # 单独一列而不是复用 concurrency：concurrency 是 Worker 自报的"我现在的上限"，
    # 两者分开才能看出"下发了但没生效"这种故障。
    desired_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                               server_default="0")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'stopping', 'stopped')",
            name="ck_worker_registry_status",
        ),
        Index("ix_worker_registry_last_seen", "last_seen"),
    )


class PipelineLedger(Base):
    __tablename__ = "pipeline_ledger"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_demand: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    overall_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    archive_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    pause_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    tasks: Mapped[List["TaskLedger"]] = relationship("TaskLedger", back_populates="pipeline", cascade="save-update, merge")

    __table_args__ = (
        CheckConstraint(
            "overall_status IN ('pending', 'running', 'success', 'aborted', 'failed')",
            name="ck_pipeline_overall_status",
        ),
        Index("ix_pipeline_ledger_status", "overall_status"),
    )


class TaskLedger(Base):
    __tablename__ = "task_ledger"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), ForeignKey("pipeline_ledger.trace_id"), nullable=False, index=True)
    target_worker: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    return_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="待拆解")
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    remaining_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    is_high_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    abort_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    depends_on: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    # Batch9：派发报文的载荷（JSON 文本，可空）。
    # 平台只搬运不解释；Worker 轮询时靠它拿到"要干什么"。
    dispatch_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Batch12：最近一次心跳时间（可空）。在线视图靠它聚合，不新增接口。
    last_heartbeat_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline: Mapped["PipelineLedger"] = relationship("PipelineLedger", back_populates="tasks")

    __table_args__ = (
        CheckConstraint(
            "status IN ('待拆解', '待调度下发', '执行中', '待验收', '验收通过', '验收驳回', '执行失败')",
            name="ck_task_status",
        ),
        Index("ix_task_ledger_trace_status", "trace_id", "status"),
    )


class MessageArchive(Base):
    __tablename__ = "message_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sender: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())

    __table_args__ = (
        CheckConstraint(
            "sender IN ('master', 'dsh', 'workbuddy', 'codex', 'user')",
            name="ck_archive_sender",
        ),
        UniqueConstraint(
            "trace_id", "task_id", "msg_type", "timestamp",
            name="uq_archive_message",
        ),
        Index("ix_message_archive_timestamp", "timestamp"),
    )
