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
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError("timestamp 必须带时区（如 +08:00），禁止 naive datetime")
        return v


# Payload models for each msg_type (minimal skeleton, business fields pass-through)
class TaskDispatchPayload(BaseModel):
    task_goal: str = Field(..., min_length=1)
    input: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Optional[Dict[str, Any]] = None
    # Batch9：派给谁（缺省按派发报文 sender 归属，见 docs/base/09）
    target_worker: Optional[Literal["master", "dsh", "workbuddy", "codex", "user"]] = None
    # Batch31-D：派给哪个岗位（instance_id，形如「角色-主机」）。为空 = 身份族内先到先得。
    target_instance: Optional[str] = Field(default=None, max_length=64)
    # Batch32-2：本单要用的插件（plugins/<name>/ 的目录名）。带它就必须带 target_instance，
    # 平台按「实例 > 岗位 > 撤权」三级判授权，未授权直接拒单（不建任务、只落审计）。
    plugin: Optional[str] = Field(default=None, max_length=64)
    # Batch9：执行规格（Worker 怎么干）。平台只搬运不解释，字段语义见 docs/base/09。
    exec: Optional[Dict[str, Any]] = None
    # Batch11：优先级（数字，越大越先）。平台只搬运不排序，排序由 Worker 自己决定。
    priority: Optional[int] = None
    # 基线要求：验收项为数组，逐条列出，不是字典
    acceptance_criteria: Optional[List[str]] = None
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
    # Batch31：显式声明「这批交付验收通过」，把该流水线下「待验收」任务推到「验收通过」。
    # 默认 False —— 老调用方不带这个字段，行为仍是只留痕、不推进状态。
    accept_tasks: bool = False
    # Batch32-1：`unreviewed`（没找到合格复核者）的单要显式覆盖才推，留痕用。
    # 只对 review_verdict=unreviewed 起作用；已 pass 的单不需要它。
    force_accept: bool = False


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
