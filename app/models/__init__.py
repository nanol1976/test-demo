from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Text, DateTime, Boolean, Integer, JSON, ForeignKey, Index, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


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
