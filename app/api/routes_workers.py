"""执行方注册表接口（Batch13）。

两个方法、一个路径：
  POST /api/workers   Worker 上报"我还活着 + 现在什么状态"（upsert，可重复调用）
  GET  /api/workers   平台返回在线列表（在线/静默/停机由平台按心跳超时判定）

为什么单独开一条路径，而不是塞进 POST /api/messages：
  报文是"任务级"的（trace_id + task_id 必填），而 Worker 存活状态跟任务无关，
  空转的 Worker 根本没有 task_id 可填。硬塞进报文会污染协议状态机。
"""
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.clock import ensure_utc
from app.core.config import get_settings
from app.core.security import require_token
from app.models import WorkerRegistry

router = APIRouter(dependencies=[Depends(require_token)])

ALLOWED_WORKERS = {"master", "dsh", "workbuddy", "codex", "user"}
ALLOWED_STATUS = {"running", "stopping", "stopped"}


class WorkerBeat(BaseModel):
    """Worker 上报体。字段都可缺省，缺省按"还在跑、并发 1"理解。"""

    worker_id: str = Field(min_length=1, max_length=64)
    status: str = "running"
    concurrency: int = Field(default=1, ge=1, le=64)
    limit: int = Field(default=1, ge=0, le=64)
    in_flight: int = Field(default=0, ge=0, le=64)
    handled: int = Field(default=0, ge=0)
    note: Optional[str] = Field(default=None, max_length=200)
    meta: Optional[dict] = None


class WorkerView(BaseModel):
    worker_id: str
    status: str
    online: bool
    last_seen: str
    first_seen: str
    age_seconds: float
    concurrency: int
    limit: int
    in_flight: int
    handled: int
    note: Optional[str] = None
    meta: Optional[dict] = None


def _view(row: WorkerRegistry, now: datetime, timeout: int) -> WorkerView:
    last_seen = ensure_utc(row.last_seen)
    age = max(0.0, (now - last_seen).total_seconds())
    online = row.status == "running" and age <= timeout
    try:
        meta = json.loads(row.meta) if row.meta else None
    except (TypeError, ValueError):
        meta = None
    return WorkerView(
        worker_id=row.worker_id,
        status=row.status,
        online=online,
        last_seen=last_seen.isoformat(),
        first_seen=ensure_utc(row.first_seen).isoformat(),
        age_seconds=round(age, 1),
        concurrency=row.concurrency,
        limit=row.limit,
        in_flight=row.in_flight,
        handled=row.handled,
        note=row.note,
        meta=meta,
    )


@router.post("/api/workers", response_model=WorkerView, status_code=202)
def report_worker(beat: WorkerBeat, db: Session = Depends(get_db)):
    """Worker 注册/心跳上报（幂等 upsert：同一 worker_id 只更新，不新增行）。"""
    if beat.worker_id not in ALLOWED_WORKERS:
        raise HTTPException(status_code=409, detail=f"worker_id 必须是 {sorted(ALLOWED_WORKERS)} 之一")
    if beat.status not in ALLOWED_STATUS:
        raise HTTPException(status_code=422, detail=f"status 必须是 {sorted(ALLOWED_STATUS)} 之一")

    now = datetime.now(timezone.utc)
    row = db.get(WorkerRegistry, beat.worker_id)
    if row is None:
        row = WorkerRegistry(
            worker_id=beat.worker_id,
            status=beat.status,
            last_seen=now,
            first_seen=now,
            concurrency=beat.concurrency,
            limit=beat.limit,
            in_flight=beat.in_flight,
            handled=beat.handled,
            note=beat.note,
            meta=json.dumps(beat.meta, ensure_ascii=False) if beat.meta else None,
        )
        db.add(row)
    else:
        row.status = beat.status
        row.last_seen = now
        row.concurrency = beat.concurrency
        row.limit = beat.limit
        row.in_flight = beat.in_flight
        row.handled = beat.handled
        row.note = beat.note
        row.meta = json.dumps(beat.meta, ensure_ascii=False) if beat.meta else None
    db.commit()
    db.refresh(row)
    return _view(row, now, get_settings().heartbeat_timeout_seconds)


@router.get("/api/workers", response_model=List[WorkerView])
def list_workers(online_only: bool = False, db: Session = Depends(get_db)):
    """在线列表：`online=true` 只看活的；默认全量（含静默/停机，便于排查"谁掉了"）。"""
    settings = get_settings()
    timeout = settings.heartbeat_timeout_seconds
    now = datetime.now(timezone.utc)
    rows = db.execute(select(WorkerRegistry).order_by(WorkerRegistry.last_seen.desc())).scalars().all()
    views = [_view(r, now, timeout) for r in rows]
    if online_only:
        views = [v for v in views if v.online]
    return views
