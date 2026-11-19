"""HTTP 入参/出参模型（Batch2）。字段名与台账列名保持一致。"""
from typing import List, Optional

from pydantic import BaseModel, Field


class MessageAccepted(BaseModel):
    archive_id: int
    status_changed: bool
    note: str = ""


class TaskView(BaseModel):
    task_id: str
    trace_id: str
    target_worker: str
    status: str
    dispatch_time: Optional[str] = None
    return_time: Optional[str] = None
    max_retry: int
    remaining_retry: int
    is_high_risk: bool
    reject_reason: Optional[str] = None
    abort_reason: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)


class PipelineView(BaseModel):
    trace_id: str
    original_demand: str
    overall_status: str
    pause_flag: bool
    archive_path: Optional[str] = None
    tasks: List[TaskView] = Field(default_factory=list)


class ArchiveView(BaseModel):
    trace_id: str
    index_rows: int
    jsonl_path: Optional[str] = None


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None
