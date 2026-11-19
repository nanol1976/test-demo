"""报文入口与查询接口（Batch2）。

只有 4 个接口：POST /api/messages + 3 个 GET。
不提供 DELETE/PUT/PATCH —— 状态只能由报文驱动。
"""
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_session_factory
from app.api.schemas_http import ArchiveView, ErrorBody, MessageAccepted, PipelineView, TaskView
from app.bus import BusBackpressureError, BusTimeoutError, MessageBus
from app.bus.router import HandlerResult, Router, UnknownMsgTypeError, VALID_MSG_TYPES
from app.core.clock import ensure_utc
from app.core.logging import get_logger
from app.core.security import require_token
from app.models import MessageArchive
from app.repositories.ledger import LedgerRepository
from app.schemas import Envelope
from app.services import ArchiveService
from app.services.dispatch import IllegalTransitionError, TaskNotFoundError, register_all

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])

_bus: Optional[MessageBus] = None
_msg_router: Optional[Router] = None


def build_message_router() -> Router:
    r = Router()
    register_all(r)
    return r


def configure(bus: MessageBus, msg_router: Router) -> None:
    """由 app 启动流程注入总线与路由表。"""
    global _bus, _msg_router
    _bus = bus
    _msg_router = msg_router


def get_bus() -> MessageBus:
    if _bus is None:
        raise HTTPException(status_code=503, detail="总线尚未启动")
    return _bus


def get_message_router() -> Router:
    if _msg_router is None:
        raise HTTPException(status_code=503, detail="报文路由表尚未装配")
    return _msg_router


def _consume(envelope: Envelope) -> HandlerResult:
    """总线消费者：每条报文一个 session，处理完即关。"""
    db = get_session_factory()()
    try:
        return get_message_router().route(db, envelope.model_dump())
    finally:
        db.close()


def _task_payload(task) -> Optional[Dict[str, Any]]:
    """派发载荷原文回读。落库是 JSON 文本，坏数据不应让整个查询接口 500，
    解析失败退化为 None（只影响这一个字段）。"""
    raw = getattr(task, "dispatch_payload", None)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("dispatch_payload 解析失败，已忽略：task_id=%s", task.task_id)
        return None
    return parsed if isinstance(parsed, dict) else None


def _task_view(task) -> TaskView:
    return TaskView(
        task_id=task.task_id,
        trace_id=task.trace_id,
        target_worker=task.target_worker,
        status=task.status,
        dispatch_time=task.dispatch_time.isoformat() if task.dispatch_time else None,
        return_time=task.return_time.isoformat() if task.return_time else None,
        max_retry=task.max_retry,
        remaining_retry=task.remaining_retry,
        is_high_risk=task.is_high_risk,
        reject_reason=task.reject_reason,
        abort_reason=task.abort_reason,
        depends_on=list(task.depends_on or []),
        payload=_task_payload(task),
        last_heartbeat_time=(ensure_utc(task.last_heartbeat_time).isoformat()
                             if task.last_heartbeat_time else None),
    )


# ---------------------------------------------------------------- POST

@router.post(
    "/messages",
    response_model=MessageAccepted,
    status_code=202,
    summary="投递一条协议 v1.0 报文",
    description=(
        "幂等键：`(trace_id, task_id, msg_type, timestamp)`。同一条报文重复投递返回同一个 "
        "`archive_id`，不会重复写归档、不会重复推进状态。\n\n"
        "状态码：202 已受理 / 400 未知 msg_type / 422 字段或 payload 校验失败 "
        "（含 naive timestamp）/ 409 当前状态不接受该报文 / 404 引用的任务或流水线不存在 / "
        "503 总线背压（消息未被接收，可重投）/ 500 台账写入失败。\n\n"
        "鉴权：配置了 `API_TOKEN` 时需带 `X-API-Token` 头，否则 401。"
    ),
    responses={400: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def post_message(raw: Dict[str, Any] = Body(...)) -> MessageAccepted:
    """收一条报文：入总线 → 路由 → 状态迁移 → 归档。"""
    msg_type = raw.get("msg_type")
    if msg_type not in VALID_MSG_TYPES:
        raise HTTPException(
            status_code=400, detail=f"未知 msg_type: {msg_type!r}（协议只接受 8 种）"
        )

    try:
        envelope = Envelope(**raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False))

    bus = get_bus()
    try:
        ticket = bus.publish(envelope.trace_id, envelope)
        result = ticket.result(timeout=bus.result_timeout)
    except BusBackpressureError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"总线繁忙，本次报文未被接收（可重投）：{exc}",
            headers={"Retry-After": "1"},
        )
    except BusTimeoutError as exc:
        raise HTTPException(status_code=503, detail=f"处理超时，请重投：{exc}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False))
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SQLAlchemyError as exc:
        logger.exception("报文落库失败")
        raise HTTPException(status_code=500, detail=f"台账写入失败：{exc}")

    return MessageAccepted(
        archive_id=result.archive_id,
        status_changed=result.status_changed,
        note=result.note,
    )


# ---------------------------------------------------------------- GET

@router.get("/pipelines/{trace_id}", response_model=PipelineView)
def get_pipeline(trace_id: str, db: Session = Depends(get_db)) -> PipelineView:
    repo = LedgerRepository(db)
    pipeline = repo.get_pipeline(trace_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail=f"流水线不存在：{trace_id}")
    return PipelineView(
        trace_id=pipeline.trace_id,
        original_demand=pipeline.original_demand,
        overall_status=pipeline.overall_status,
        pause_flag=pipeline.pause_flag,
        archive_path=pipeline.archive_path,
        tasks=[_task_view(t) for t in repo.list_tasks(trace_id)],
    )


@router.get("/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskView:
    task = LedgerRepository(db).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return _task_view(task)


@router.get("/archive/{trace_id}", response_model=ArchiveView)
def get_archive(trace_id: str, db: Session = Depends(get_db)) -> ArchiveView:
    """只报索引行数与 JSONL 路径，不返回原始报文正文。"""
    rows = db.execute(
        select(MessageArchive).where(MessageArchive.trace_id == trace_id)
    ).scalars().all()
    path = None
    if rows:
        path = str(ArchiveService(db).get_archive_path(trace_id) or "")
    return ArchiveView(trace_id=trace_id, index_rows=len(rows), jsonl_path=path)


__all__ = ["router", "configure", "build_message_router", "_consume"]

