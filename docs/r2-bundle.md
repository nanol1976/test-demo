# 现状代码合集 + 整改要求（一次读完）

## FILE: app/schemas/__init__.py
```python
from datetime import datetime
from typing import Optional, Any, Dict, List, Literal
from pydantic import BaseModel, Field, field_validator
from enum import Enum


class MsgType(str, Enum):
    TASK_DISPATCH = "task_dispatch"
    TASK_HEARTBEAT = "task_heartbeat"
    TASK_RETURN = "task_return"
    TASK_REJECT = "task_reject"
    TASK_ABORT = "task_abort"
    HUMAN_CONFIRM_REQ = "human_confirm_req"
    HUMAN_CONFIRM_RESP = "human_confirm_resp"
    TASK_SUMMARY = "task_summary"


class Sender(str, Enum):
    MASTER = "master"
    DSH = "dsh"
    WORKBUDDY = "workbuddy"
    CODEX = "codex"
    USER = "user"


class Envelope(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=64)
    task_id: str = Field(..., min_length=1, max_length=64)
    msg_type: MsgType
    sender: Sender
    timestamp: datetime
    version: Literal["v1.0"] = "v1.0"
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v):
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


# Payload models for each msg_type (minimal skeleton, business fields pass-through)
class TaskDispatchPayload(BaseModel):
    task_goal: str = ""
    input: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Optional[Dict[str, Any]] = None
    acceptance_criteria: Optional[Dict[str, Any]] = None
    depends_on: List[str] = Field(default_factory=list)
    max_retry: int = 3
    is_high_risk: bool = False


class TaskHeartbeatPayload(BaseModel):
    progress: Optional[float] = None
    message: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskReturnPayload(BaseModel):
    output: Dict[str, Any] = Field(default_factory=dict)
    logs: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskRejectPayload(BaseModel):
    reason: str
    details: Optional[Dict[str, Any]] = None


class TaskAbortPayload(BaseModel):
    reason: str


class HumanConfirmReqPayload(BaseModel):
    summary: str
    risk_level: str = "high"
    context: Dict[str, Any] = Field(default_factory=dict)


class HumanConfirmRespPayload(BaseModel):
    approved: bool
    comment: str = ""


class TaskSummaryPayload(BaseModel):
    pipeline_status: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    summary: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Map msg_type to payload model
PAYLOAD_MODELS: Dict[MsgType, type] = {
    MsgType.TASK_DISPATCH: TaskDispatchPayload,
    MsgType.TASK_HEARTBEAT: TaskHeartbeatPayload,
    MsgType.TASK_RETURN: TaskReturnPayload,
    MsgType.TASK_REJECT: TaskRejectPayload,
    MsgType.TASK_ABORT: TaskAbortPayload,
    MsgType.HUMAN_CONFIRM_REQ: HumanConfirmReqPayload,
    MsgType.HUMAN_CONFIRM_RESP: HumanConfirmRespPayload,
    MsgType.TASK_SUMMARY: TaskSummaryPayload,
}


def validate_envelope(envelope: Envelope) -> Envelope:
    """Validate envelope and its payload against msg_type schema."""
    payload_model = PAYLOAD_MODELS.get(envelope.msg_type)
    if payload_model:
        validated_payload = payload_model(**envelope.payload)
        envelope.payload = validated_payload.model_dump()
    return envelope

```

## FILE: app/models/__init__.py
```python
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Text, DateTime, Boolean, Integer, JSON, ForeignKey, Index
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
        Index("ix_pipeline_ledger_status", "overall_status"),
    )


class TaskLedger(Base):
    __tablename__ = "task_ledger"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), ForeignKey("pipeline_ledger.trace_id"), nullable=False, index=True)
    target_worker: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    return_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    remaining_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    is_high_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    abort_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    depends_on: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)

    pipeline: Mapped["PipelineLedger"] = relationship("PipelineLedger", back_populates="tasks")

    __table_args__ = (
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
        Index("ix_message_archive_timestamp", "timestamp"),
    )

```

## FILE: app/services/__init__.py
```python
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from app.models import MessageArchive, Base
from app.schemas import Envelope
from app.core.config import get_settings


class ArchiveService:
    def __init__(self, db: Session, archive_root: Optional[str] = None):
        self.db = db
        self.archive_root = Path(archive_root or get_settings().archive_root)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def _get_jsonl_path(self, trace_id: str) -> Path:
        """Get JSONL file path for a trace_id."""
        # Organize by date for easier management
        date_str = datetime.now().strftime("%Y%m%d")
        return self.archive_root / date_str / f"{trace_id}.jsonl"

    def append_archive(self, trace_id: str, task_id: Optional[str], envelope: Envelope) -> MessageArchive:
        """
        Append envelope to JSONL file and create MessageArchive index row.
        Returns the created MessageArchive record.
        """
        jsonl_path = self._get_jsonl_path(trace_id)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize envelope to JSON
        raw_json = envelope.model_dump_json()

        # Append to JSONL file
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(raw_json + "\n")

        # Create MessageArchive index record
        archive_record = MessageArchive(
            trace_id=trace_id,
            task_id=task_id,
            msg_type=envelope.msg_type.value,
            sender=envelope.sender.value,
            timestamp=envelope.timestamp,
            raw_json=raw_json,
        )

        self.db.add(archive_record)
        self.db.commit()
        self.db.refresh(archive_record)

        return archive_record

    def get_archive_path(self, trace_id: str) -> Optional[Path]:
        """Get the JSONL file path for a trace_id if it exists."""
        jsonl_path = self._get_jsonl_path(trace_id)
        return jsonl_path if jsonl_path.exists() else None

```

## FILE: app/api/deps.py
```python
"""FastAPI dependencies for Master-Worker Platform."""
from typing import Generator
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings


def get_engine():
    """Create and return SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(settings.database_url, echo=settings.log_level == "DEBUG")


def get_session_factory():
    """Return a session factory."""
    engine = get_engine()
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()

```

## FILE: app/core/config.py
```python
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/mw_platform.db"
    archive_root: str = "./archive"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()

```

## FILE: scripts/init_db.py
```python
#!/usr/bin/env python3
"""Database initialization script.
Creates tables and indexes for the Master-Worker Platform.
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Base, PipelineLedger, TaskLedger, MessageArchive


def init_database(demo: bool = False):
    settings = get_settings()
    database_url = settings.database_url
    
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    # Use settings.debug to control echo
    engine = create_engine(database_url, echo=settings.log_level == "DEBUG")
    
    Base.metadata.create_all(engine)
    
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Created tables: {tables}")
    
    for table in tables:
        indexes = inspector.get_indexes(table)
        print(f"Indexes for {table}: {indexes}")
    
    if demo:
        with Session(engine) as session:
            pipeline = PipelineLedger(
                trace_id="test-trace-001",
                original_demand="Test demand",
                overall_status="pending"
            )
            session.add(pipeline)
            session.commit()
            print(f"Inserted PipelineLedger: {pipeline.trace_id}")
            
            task = TaskLedger(
                task_id="test-task-001",
                trace_id="test-trace-001",
                target_worker="dsh",
                status="pending",
                max_retry=3,
                remaining_retry=3,
                is_high_risk=False,
                depends_on=[]
            )
            session.add(task)
            session.commit()
            print(f"Inserted TaskLedger: {task.task_id}")
            
            from datetime import datetime, timezone
            archive = MessageArchive(
                trace_id="test-trace-001",
                task_id="test-task-001",
                msg_type="task_dispatch",
                sender="master",
                timestamp=datetime.now(timezone.utc),
                raw_json="{\"trace_id\":\"test-trace-001\",\"task_id\":\"test-task-001\",\"msg_type\":\"task_dispatch\",\"sender\":\"master\",\"timestamp\":\"2024-01-01T00:00:00+00:00\",\"version\":\"v1.0\",\"payload\":{}}"
            )
            session.add(archive)
            session.commit()
            print(f"Inserted MessageArchive: {archive.id}")
            
            pipelines = session.query(PipelineLedger).all()
            tasks = session.query(TaskLedger).all()
            archives = session.query(MessageArchive).all()
            print(f"Pipelines: {len(pipelines)}, Tasks: {len(tasks)}, Archives: {len(archives)}")
    
    print("Database initialization completed successfully!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize database")
    parser.add_argument("--demo", action="store_true", help="Insert demo data (default: False)")
    args = parser.parse_args()
    
    try:
        init_database(demo=args.demo)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

```

## FILE: app/api/__init__.py
```python
"""API package for Master-Worker Platform."""
from app.api.routes_health import router as health_router

__all__ = ["health_router"]

```

# 整改要求
# Batch1 修正单 R2（双端评审合并后的一次性整改）

来源：Mac 端 Codex 独立评审 + 主审静态走查/动态验证，合并裁定后一次性下发。
解释器固定 `.venv311\Scripts\python.exe`，禁止 pip install、禁止新建 venv。

## 一、必须修（P0，清不完不许进 Batch2）

1. `app/schemas/__init__.py:48`
   `acceptance_criteria` 现为 `Optional[Dict[str, Any]]`，基线是**数组**。
   → 改为 `Optional[List[str]]`，并在 docstring 说明"逐条验收项"。

2. `app/api/deps.py:9-18`
   `get_engine()` / `get_session_factory()` 每次请求新建 engine（连接池泄漏）。
   → 改为模块级 `@lru_cache` 单例，`get_db()` 只负责 yield session 并 close。

3. `app/models/__init__.py` PipelineLedger
   `overall_status` 加约束：只允许 `pending / running / success / aborted / failed`。
   → `CheckConstraint`，写进 `__table_args__`。

4. `app/models/__init__.py` TaskLedger
   `status` 加约束：只允许 7 种状态
   `待拆解 / 待调度下发 / 执行中 / 待验收 / 验收通过 / 验收驳回 / 执行失败`。
   → 同上，用 CheckConstraint。

## 二、应修（P1）

5. 新增 `app/repositories/ledger.py`
   - `create_pipeline(...)` / `create_task(...)`
   - `update_task_status(...)` / `update_pipeline_status(...)`
   - `get_pipeline(trace_id)` / `get_task(task_id)` / `list_tasks(trace_id)`
   - **不得提供** delete / truncate / bulk_update / upsert-覆盖
   - 文件顶部注释写明"台账只追加与状态更新，禁止删除覆盖"

6. `app/schemas/__init__.py` Envelope.timestamp
   validator 拒绝 naive datetime（无 tzinfo 直接抛错，提示必须带时区）。

7. `app/schemas/__init__.py` TaskDispatchPayload.task_goal
   → `Field(..., min_length=1)`，去掉默认空串。

8. `app/models/__init__.py` MessageArchive
   加唯一约束 `(trace_id, task_id, msg_type, timestamp)`，防止重发产生重复索引行。

9. `app/services/__init__.py:22`
   归档目录日期从 `datetime.now()` 改为 `datetime.now(timezone.utc)`，与报文时区一致。

## 三、建议修（P2）

10. `MessageArchive.sender` 加 CheckConstraint，5 值：master/dsh/workbuddy/codex/user。
11. `app/core/config.py` 用 `model_config = SettingsConfigDict(...)` 替换 `class Config`。
12. `scripts/init_db.py` demo 数据里的 raw_json 改用 `Envelope(...).model_dump_json()` 生成，不手工拼串。
13. 补 `.gitignore`：`.venv*/`、`__pycache__/`、`data/*.db`、`archive/`、`*.log`。

## 四、明确禁止改动的（评审已否决）

- ❌ 不要把 `Envelope.task_id` 改成 Optional。基线 3.1 规定所有报文必带 trace_id + task_id。
- ❌ 不要把 `default=list` 改成 `default_factory=list`。SQLAlchemy `mapped_column(default=list)` 传的是可调用对象，本来就是对的。
- ❌ 不要提前实现状态机流转校验、pause/resume、依赖调度器 —— 属于 Batch2/Batch4，不在本批次范围。

## 五、改完自检（必须真跑并贴输出）

```
.venv311\Scripts\python.exe -c 'import app.models,app.schemas,app.services,app.repositories.ledger,app.main; print(1)'
.venv311\Scripts\python.exe -c 'from app.schemas import Envelope; e=Envelope(trace_id="t",task_id="k",msg_type="task_dispatch",sender="master",timestamp="2026-11-19T10:00:00+08:00"); print(type(e))'
.venv311\Scripts\python.exe -c 'from app.schemas import Envelope; Envelope(trace_id="t",task_id="k",msg_type="task_dispatch",sender="master",timestamp="2026-11-19T10:00:00"); print("naive 未拒绝")'
.venv311\Scripts\python.exe scripts\init_db.py
```
第二条要成功；第三条要报错（naive datetime 被拒）。

写文件规则不变：一次一个文件，单条 here-string，内容禁止出现转义双引号，写完 `Get-Content` 校验。

