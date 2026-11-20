"""运维审计查询接口（Batch18，第 11 个路径）。

Batch16 把审计落成了表，但只能靠 CLI 读。看板要回答"最近一次下发是什么、到底生效没有"，
必须给它一条只读路径。这是**唯一一次主动扩接口面**（10 → 11），已在需求里明确同意。

设计：
  GET /api/audit?limit=20&worker_id=&instance_id=&command=
  - `items`：审计流水（倒序），与注册表里当前值比对后给出 `effective`
      * `true`  = 这次下发就是当前生效值，且 Worker 自报的 concurrency 已等于它
      * `false` = 已是当前期望值，但 Worker 还没按它跑（下发没生效）
      * `null`  = 已被更晚的下发覆盖（历史行），或这条本来就没有"生效"概念（暂停/恢复/复位熔断）
  - `current`：注册表快照（`worker_id/instance_id` → `desired_limit` / `concurrency` / `online`）

只读：不写库、不改状态，也不提供删除。审计是 append-only，接口也必须是只读的。
"""
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.clock import ensure_utc
from app.core.config import get_settings
from app.core.security import require_token
from app.models import WorkerQuotaAudit, WorkerRegistry

router = APIRouter(dependencies=[Depends(require_token)])


class AuditItem(BaseModel):
    id: int
    command: str
    worker_id: str
    instance_id: str
    prev_limit: int
    desired_limit: int
    reason: Optional[str] = None
    created_at: str
    effective: Optional[bool] = None


class AuditMeta(BaseModel):
    total: int
    shown: int
    limit: int


class AuditView(BaseModel):
    meta: AuditMeta
    # 注册表快照：key = "worker_id/instance_id"
    current: Dict[str, dict] = Field(default_factory=dict)
    items: List[AuditItem] = Field(default_factory=list)


@router.get(
    "/api/audit",
    response_model=AuditView,
    summary="运维审计流水（配额下发 / 暂停 / 恢复 / 复位熔断）",
    description=(
        "倒序返回审计流水，并与注册表当前值比对给出 effective。"
        "只读接口：不写库、不删行。审计表本身 append-only。"
    ),
)
def list_audit(limit: int = Query(default=20, ge=1, le=200),
               worker_id: Optional[str] = Query(default=None, max_length=64),
               instance_id: Optional[str] = Query(default=None, max_length=64),
               command: Optional[str] = Query(default=None, max_length=64),
               db: Session = Depends(get_db)) -> AuditView:
    settings = get_settings()
    timeout = settings.heartbeat_timeout_seconds
    now = datetime.now().astimezone()

    stmt = select(WorkerQuotaAudit).order_by(WorkerQuotaAudit.id.desc())
    count_stmt = select(WorkerQuotaAudit)
    if worker_id:
        stmt = stmt.where(WorkerQuotaAudit.worker_id == worker_id)
        count_stmt = count_stmt.where(WorkerQuotaAudit.worker_id == worker_id)
    if instance_id:
        stmt = stmt.where(WorkerQuotaAudit.instance_id == instance_id)
        count_stmt = count_stmt.where(WorkerQuotaAudit.instance_id == instance_id)
    if command:
        stmt = stmt.where(WorkerQuotaAudit.command == command)
        count_stmt = count_stmt.where(WorkerQuotaAudit.command == command)

    rows = db.execute(stmt.limit(limit)).scalars().all()
    total = len(db.execute(count_stmt).scalars().all())

    reg = db.execute(select(WorkerRegistry)).scalars().all()
    current: Dict[str, dict] = {}
    reg_map = {}
    for r in reg:
        key = f"{r.worker_id}/{r.instance_id}"
        age = max(0.0, (now - ensure_utc(r.last_seen)).total_seconds())
        current[key] = {
            "desired_limit": r.desired_limit,
            "concurrency": r.concurrency,
            "online": r.status == "running" and age <= timeout,
        }
        reg_map[(r.worker_id, r.instance_id)] = r

    # "最新一条配额下发"才算有"生效"概念；更早的历史行一律 null
    latest_quota = {}
    for row in db.execute(select(WorkerQuotaAudit)
                          .where(WorkerQuotaAudit.command == "master:worker-limit")
                          .order_by(WorkerQuotaAudit.id.desc())).scalars().all():
        key = (row.worker_id, row.instance_id)
        if key not in latest_quota:
            latest_quota[key] = row.id

    items: List[AuditItem] = []
    for row in rows:
        effective: Optional[bool] = None
        if row.command == "master:worker-limit":
            key = (row.worker_id, row.instance_id)
            if latest_quota.get(key) == row.id:
                cur = reg_map.get(key)
                if cur is None:
                    effective = False
                elif int(cur.desired_limit) != int(row.desired_limit):
                    effective = False
                elif int(row.desired_limit) == 0:
                    # 撤销：注册表已回到"未下发，所以没有比对基准，只认 desired 已归零
                    effective = True
                else:
                    effective = int(cur.concurrency) == int(row.desired_limit)
        items.append(AuditItem(
            id=row.id,
            command=row.command,
            worker_id=row.worker_id,
            instance_id=row.instance_id,
            prev_limit=row.prev_limit,
            desired_limit=row.desired_limit,
            reason=row.reason,
            created_at=ensure_utc(row.created_at).isoformat() if row.created_at else "",
            effective=effective,
        ))

    return AuditView(meta=AuditMeta(total=total, shown=len(items), limit=limit),
                     current=current, items=items)
