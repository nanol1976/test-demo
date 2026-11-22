"""HTTP 入参/出参模型（Batch2）。字段名与台账列名保持一致。"""
from typing import Any, Dict, List, Optional

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
    # Batch9：派发载荷原文（原样搬运，平台不解释；老数据为 None）
    payload: Optional[Dict[str, Any]] = None
    # Batch12：最近一次心跳时间（在线视图用；从未心跳为 None）
    last_heartbeat_time: Optional[str] = None
    # Batch31-D：派给哪个岗位（未指定为 None = 身份族内先到先得）
    target_instance: Optional[str] = None
    # Batch31-D：实际由哪个岗位完成（回执未带 instance_id 时为 None）
    done_instance: Optional[str] = None
    # Batch32-1：异构复核（字段不是路径——接口仍是 11 条，只是返回体多三个字段）
    # review_of：本行是复核单时指向被复核的 task_id
    review_of: Optional[str] = None
    # review_verdict：被复核任务的结论 pass / fail / unreviewed
    review_verdict: Optional[str] = None
    # review_items：逐项结论（原文 JSON 解析后的数组）
    review_items: List[Dict[str, Any]] = Field(default_factory=list)
    # Batch33-2：产物留档（字段不是路径——接口仍是 11 条，只是返回体多三个字段）
    # artifact：本机能看到的产物相对路径（看不到就是 None，不编路径）
    artifact: Optional[str] = None
    artifact_bytes: Optional[int] = None
    # artifact_error：Worker 写档失败时的原因（写不进去不影响这一单的结论）
    artifact_error: Optional[str] = None


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


class PageMeta(BaseModel):
    total: int
    page: int
    size: int
    pages: int


class PipelineSummary(BaseModel):
    trace_id: str
    original_demand: str
    overall_status: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    pause_flag: bool


class PipelineSummaryPage(BaseModel):
    meta: PageMeta
    items: List[PipelineSummary] = Field(default_factory=list)


class TaskPage(BaseModel):
    meta: PageMeta
    items: List[TaskView] = Field(default_factory=list)


class ExportPayload(BaseModel):
    trace_id: str
    original_demand: str
    overall_status: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    tasks: List[TaskView] = Field(default_factory=list)
