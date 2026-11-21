"""台账只读查询接口（Batch3）。

只有 GET：分页 / 筛选 / 导出视图。不提供任何写入、状态修改、删除。
"""
import csv
import io
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.routes_messages import _task_view
from app.api.schemas_http import (
    ExportPayload,
    PageMeta,
    PipelineSummary,
    PipelineSummaryPage,
    TaskPage,
    TaskView,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import require_token
from app.repositories.ledger import LedgerRepository

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])


def _normalize_page(page: int, size: int) -> tuple:
    settings = get_settings()
    if page < 1:
        raise HTTPException(status_code=422, detail="page 必须 >= 1")
    if size < 1 or size > settings.page_size_max:
        raise HTTPException(
            status_code=422, detail=f"size 必须在 1..{settings.page_size_max} 之间"
        )
    return page, size


def _summary(pipeline) -> PipelineSummary:
    return PipelineSummary(
        trace_id=pipeline.trace_id,
        original_demand=pipeline.original_demand,
        overall_status=pipeline.overall_status,
        start_time=pipeline.start_time.isoformat() if pipeline.start_time else None,
        end_time=pipeline.end_time.isoformat() if pipeline.end_time else None,
        pause_flag=pipeline.pause_flag,
    )


@router.get(
    "/pipelines",
    response_model=PipelineSummaryPage,
    summary="流水线列表（分页 + 状态/时间/关键字筛选 + 排序）",
    description=(
        "只读查询。status 只接受 pending/running/success/aborted/failed；"
        "start_from / start_to 为带时区的 ISO 时间，按 start_time 过滤；"
        "q 在 trace_id 与原始需求里做子串匹配；"
        "sort 只接受 start_time_desc（默认）/ start_time_asc。"
    ),
)
def list_pipelines(
    status: Optional[str] = Query(default=None, description="流水线状态白名单值"),
    start_from: Optional[datetime] = Query(default=None, description="起始时间下界（含）"),
    start_to: Optional[datetime] = Query(default=None, description="起始时间上界（含）"),
    q: Optional[str] = Query(default=None, max_length=128, description="关键字（trace_id / 原始需求子串）"),
    sort: str = Query(default="start_time_desc", description="排序：start_time_desc / start_time_asc"),
    page: int = Query(default=1, description="页码，从 1 开始"),
    size: Optional[int] = Query(default=None, description="每页条数，默认取配置值"),
    db: Session = Depends(get_db),
) -> PipelineSummaryPage:
    page, size = _normalize_page(page, size or get_settings().page_size_default)
    repo = LedgerRepository(db)
    keyword = (q or "").strip() or None
    try:
        total = repo.count_pipelines(status, start_from, start_to, keyword)
        items = repo.list_pipelines(
            status, start_from, start_to, keyword, sort, limit=size, offset=(page - 1) * size
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return PipelineSummaryPage(
        meta=PageMeta(total=total, page=page, size=size, pages=max(1, -(-total // size))),
        items=[_summary(p) for p in items],
    )


@router.get(
    "/tasks",
    response_model=TaskPage,
    summary="任务列表（分页 + trace/状态/执行方/关键字筛选 + 排序）",
    description="只读查询。status 只接受 7 个中文任务状态；q 在 task_id 里做子串匹配；sort 只接受 task_id_asc（默认）/ task_id_desc。",
)
def list_tasks(
    trace_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    target_worker: Optional[str] = Query(default=None),
    instance_id: Optional[str] = Query(
        default=None,
        max_length=64,
        description="按「谁在问」收敛：传了实例名，只返回未指定岗位的任务 + 指定给该岗位的任务（Batch31-D）",
    ),
    q: Optional[str] = Query(default=None, max_length=128),
    include_all: bool = Query(
        default=False,
        description="管理视图：true 时不过滤指定岗位的任务（默认 false，未说明身份只能看到不指定岗位的活）",
    ),
    sort: str = Query(default="task_id_asc"),
    page: int = Query(default=1),
    size: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
) -> TaskPage:
    page, size = _normalize_page(page, size or get_settings().page_size_default)
    repo = LedgerRepository(db)
    keyword = (q or "").strip() or None
    try:
        total = repo.count_tasks(
            trace_id, status, target_worker, keyword, instance_id, include_all
        )
        items = repo.search_tasks(
            trace_id, status, target_worker, keyword, sort,
            limit=size, offset=(page - 1) * size, instance_id=instance_id,
            include_all=include_all,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return TaskPage(
        meta=PageMeta(total=total, page=page, size=size, pages=max(1, -(-total // size))),
        items=[_task_view(t) for t in items],
    )


@router.get(
    "/export/{trace_id}",
    response_model=ExportPayload,
    responses={200: {"content": {"text/csv": {}}}},
    summary="导出一条流水线的台账快照（json 或 csv）",
    description=(
        "只读导出视图，不改变任何状态。format=json 返回结构体；"
        "format=csv 返回 text/csv 文本（表头固定，便于下游核对）。"
    ),
)
def export_trace(
    trace_id: str,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
):
    repo = LedgerRepository(db)
    pipeline = repo.get_pipeline(trace_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail=f"流水线不存在：{trace_id}")
    tasks = repo.list_tasks(trace_id)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "trace_id", "task_id", "target_worker", "status",
                "dispatch_time", "return_time", "max_retry", "remaining_retry",
                "is_high_risk", "reject_reason", "abort_reason",
            ]
        )
        for t in tasks:
            writer.writerow(
                [
                    t.trace_id, t.task_id, t.target_worker, t.status,
                    t.dispatch_time.isoformat() if t.dispatch_time else "",
                    t.return_time.isoformat() if t.return_time else "",
                    t.max_retry, t.remaining_retry, int(t.is_high_risk),
                    t.reject_reason or "", t.abort_reason or "",
                ]
            )
        return PlainTextResponse(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{trace_id}.csv"'},
        )
    return ExportPayload(
        trace_id=pipeline.trace_id,
        original_demand=pipeline.original_demand,
        overall_status=pipeline.overall_status,
        start_time=pipeline.start_time.isoformat() if pipeline.start_time else None,
        end_time=pipeline.end_time.isoformat() if pipeline.end_time else None,
        tasks=[_task_view(t) for t in tasks],
    )
