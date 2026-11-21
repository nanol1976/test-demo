"""运维审计查询接口（Batch18，第 11 个路径）。

Batch16 把审计落成了表，但只能靠 CLI 读。看板要回答"最近一次下发是什么、到底生效没有"，
必须给它一条只读路径。这是**唯一一次主动扩接口面**（10 → 11），已在需求里明确同意。

设计：
  GET /api/audit?limit=20&offset=0&worker_id=&instance_id=&command=
  - `items`：审计流水（倒序），与注册表里当前值比对后给出 `effective`
      * `true`  = 这次下发就是当前生效值，且 Worker 自报的 concurrency 已等于它
      * `false` = 已是当前期望值，但 Worker 还没按它跑（下发没生效）
      * `null`  = 已被更晚的下发覆盖（历史行），或这条本来就没有"生效"概念（暂停/恢复/复位熔断）
  - `current`：注册表快照（`worker_id/instance_id` → `desired_limit` / `concurrency` / `online`）

Batch22：加 `offset` 支持真分页（仍是同一条路径，接口面仍然是 11）；
每条记录带 `operator`（X-Operator 自报，可能为空，不是身份）。

Batch32-2：把 `plugin_audit`（插件授权判定）也并进这条流水，command 记 `plugin:allow` /
`plugin:deny`，并额外带 `plugin` / `decision` 两字段。两条表按时间合并后统一分页，
路径不变（仍是 11 条）。插件行没有配额概念，`prev_limit` / `desired_limit` 为 null，
`effective` 也是 null——看板按 `decision` 显示「已放行 / 已拒」。

只读：不写库、不改状态，也不提供删除。审计是 append-only，接口也必须是只读的。
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.clock import ensure_utc
from app.core.config import get_settings
from app.core.security import require_token
from app.models import PluginAudit, WorkerQuotaAudit, WorkerRegistry

router = APIRouter(dependencies=[Depends(require_token)])


class AuditItem(BaseModel):
    id: int
    command: str
    worker_id: str
    instance_id: str
    # 配额下发才有这两个值；插件授权判定（plugin:*）没有配额概念，留 None
    prev_limit: Optional[int] = None
    desired_limit: Optional[int] = None
    operator: str = ""
    reason: Optional[str] = None
    created_at: str
    effective: Optional[bool] = None
    # Batch32-2：插件授权行额外带插件名与判定结论（deny / allow）
    plugin: Optional[str] = None
    decision: Optional[str] = None


class AuditMeta(BaseModel):
    total: int
    shown: int
    limit: int


class AuditView(BaseModel):
    meta: AuditMeta
    # 注册表快照：key = "worker_id/instance_id"
    current: Dict[str, dict] = Field(default_factory=dict)
    items: List[AuditItem] = Field(default_factory=list)


def _stamp(value) -> str:
    return ensure_utc(value).isoformat() if value else ""


def _sort_key(created, source: int, row_id: int):
    """跨两张表排序：时间优先，同时间按来源与 id 兜底（都是倒序）。"""
    dt = ensure_utc(created) if created else datetime.min.replace(tzinfo=timezone.utc)
    return (dt, source, row_id)


@router.get(
    "/api/audit",
    response_model=AuditView,
    summary="运维审计流水（配额下发 / 暂停 / 恢复 / 复位熔断 / 插件授权判定）",
    description=(
        "倒序返回审计流水（配额审计 + 插件授权审计合并分页），并与注册表当前值比对给出 effective。"
        "只读接口：不写库、不删行。审计表本身 append-only。"
    ),
)
def list_audit(limit: int = Query(default=20, ge=1, le=200),
               offset: int = Query(default=0, ge=0, le=100000,
                                   description="跳过多少条，用于翻页；items 本身仍是倒序"),
               worker_id: Optional[str] = Query(default=None, max_length=64),
               instance_id: Optional[str] = Query(default=None, max_length=64),
               command: Optional[str] = Query(default=None, max_length=64),
               db: Session = Depends(get_db)) -> AuditView:
    settings = get_settings()
    timeout = settings.heartbeat_timeout_seconds
    now = datetime.now().astimezone()
    take = offset + limit

    # 过滤口径：command 以 plugin: 开头就只看插件审计，否则只看配额审计；
    # 不给 command 则两张表都算，合并后再分页。
    want_plugin = command is None or command.startswith("plugin:")
    want_quota = command is None or not command.startswith("plugin:")

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

    entries = []  # (sort_key, AuditItem)
    total = 0

    if want_quota:
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

        rows = db.execute(stmt.limit(take)).scalars().all()
        total += len(db.execute(count_stmt).scalars().all())
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
            item = AuditItem(
                id=row.id,
                command=row.command,
                worker_id=row.worker_id,
                instance_id=row.instance_id,
                prev_limit=row.prev_limit,
                desired_limit=row.desired_limit,
                operator=getattr(row, "operator", "") or "",
                reason=row.reason,
                created_at=_stamp(row.created_at),
                effective=effective,
            )
            entries.append((_sort_key(row.created_at, 0, row.id), item))

    if want_plugin:
        p_stmt = select(PluginAudit).order_by(PluginAudit.id.desc())
        p_count = select(PluginAudit)
        if worker_id:
            p_stmt = p_stmt.where(PluginAudit.worker_id == worker_id)
            p_count = p_count.where(PluginAudit.worker_id == worker_id)
        if instance_id:
            p_stmt = p_stmt.where(PluginAudit.instance_id == instance_id)
            p_count = p_count.where(PluginAudit.instance_id == instance_id)
        if command:
            decision = command.split(":", 1)[1] if ":" in command else ""
            if decision in ("allow", "deny"):
                p_stmt = p_stmt.where(PluginAudit.decision == decision)
                p_count = p_count.where(PluginAudit.decision == decision)
            else:
                # 未知的插件动作名：按"没有匹配"处理，而不是悄悄返回全部
                p_stmt = p_stmt.where(PluginAudit.id < 0)
                p_count = p_count.where(PluginAudit.id < 0)

        p_rows = db.execute(p_stmt.limit(take)).scalars().all()
        total += len(db.execute(p_count).scalars().all())
        for row in p_rows:
            item = AuditItem(
                id=row.id,
                command="plugin:%s" % row.decision,
                worker_id=row.worker_id,
                instance_id=row.instance_id,
                prev_limit=None,
                desired_limit=None,
                operator="",
                reason=row.reason,
                created_at=_stamp(row.created_at),
                effective=None,
                plugin=row.plugin_name,
                decision=row.decision,
            )
            entries.append((_sort_key(row.created_at, 1, row.id), item))

    entries.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _key, item in entries[offset:offset + limit]]

    return AuditView(meta=AuditMeta(total=total, shown=len(items), limit=limit),
                     current=current, items=items)
