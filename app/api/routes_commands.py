"""运维指令接口（Batch4；Batch15 增加实例级并发配额下发）。

只接受 master: 前缀指令，不做任何业务决策：
- master:pause          暂停该 trace（拦截后续派发）
- master:resume         恢复该 trace（并复位熔断）
- master:clear-breaker  仅复位熔断标志，不改暂停状态
- master:worker-limit   给某个执行方实例下发期望并发上限（Batch15）

Batch15 的动机：Worker 自报的 concurrency 只是"我现在开几个"，平台没法要求它开几个。
弹性并发的上限如果只能靠改命令行重启进程，就没法在运行中收放。这里复用同一条指令通道
（不新增路径、方法仍是 POST），让平台把 desired_limit 写进注册表，Worker 在下一次
注册表上报的响应里读到并立即生效。limit=0 表示撤销下发、恢复 Worker 自己的配置。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.logging import get_logger
from app.core.security import require_token
from app.models import WorkerQuotaAudit, WorkerRegistry
from app.repositories.ledger import LedgerRepository
from app.services.breaker import breaker

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])

ALLOWED_COMMANDS = {"master:pause", "master:resume", "master:clear-breaker", "master:worker-limit"}


class CommandRequest(BaseModel):
    command: str = Field(..., description="master:pause | master:resume | master:clear-breaker | master:worker-limit")
    trace_id: Optional[str] = Field(default=None, max_length=64,
                                    description="流水线指令必填；master:worker-limit 可不填")
    # Batch15：实例级并发配额
    worker_id: Optional[str] = Field(default=None, max_length=64)
    instance_id: Optional[str] = Field(default=None, max_length=64)
    limit: Optional[int] = Field(default=None, ge=0, le=64,
                                description="期望并发上限；0 = 撤销下发")
    reason: Optional[str] = None


class CommandResult(BaseModel):
    trace_id: Optional[str] = None
    command: str
    paused: bool = False
    breaker_open: bool = False
    # Batch15：下发配额时回显，便于确认"写进去了"
    worker_id: Optional[str] = None
    instance_id: Optional[str] = None
    desired_limit: Optional[int] = None


@router.post(
    "/commands",
    response_model=CommandResult,
    summary="下发运维指令（暂停 / 恢复 / 复位熔断 / 实例并发配额）",
    description=(
        "只接受 master:pause、master:resume、master:clear-breaker、master:worker-limit 四条指令。"
        "暂停只会拦截该 trace 的后续派发，不改变任何任务的既有状态。"
        "master:worker-limit 只写注册表的 desired_limit，由 Worker 下次上报时自行生效，"
        "平台不会去杀在飞的任务。"
    ),
)
def post_command(payload: CommandRequest, db: Session = Depends(get_db)) -> CommandResult:
    if payload.command not in ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"未知指令: {payload.command}")

    if payload.command == "master:worker-limit":
        return _worker_limit(payload, db)

    if not payload.trace_id:
        raise HTTPException(status_code=422, detail=f"{payload.command} 必须带 trace_id")
    repo = LedgerRepository(db)
    pipeline = repo.get_pipeline(payload.trace_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail=f"流水线不存在：{payload.trace_id}")

    if payload.command == "master:pause":
        pipeline = repo.set_pause(payload.trace_id, True)
    elif payload.command == "master:resume":
        pipeline = repo.set_pause(payload.trace_id, False)
        breaker.reset(payload.trace_id)
    else:
        breaker.reset(payload.trace_id)

    logger.info(
        "运维指令：%s trace=%s reason=%s", payload.command, payload.trace_id, payload.reason
    )
    return CommandResult(
        trace_id=payload.trace_id,
        command=payload.command,
        paused=bool(pipeline.pause_flag),
        breaker_open=breaker.is_open(payload.trace_id),
    )


def _worker_limit(payload: CommandRequest, db: Session) -> CommandResult:
    """写 desired_limit。找不到实例就 404 —— 不给没上报过的实例凭空建行，
    否则注册表会变成"谁都能往里塞"的假台账。"""
    if not payload.worker_id:
        raise HTTPException(status_code=422, detail="master:worker-limit 必须带 worker_id")
    if payload.limit is None:
        raise HTTPException(status_code=422, detail="master:worker-limit 必须带 limit（0=撤销）")

    instance_id = payload.instance_id or "default"
    row = db.get(WorkerRegistry, (payload.worker_id, instance_id))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"执行方实例不存在：{payload.worker_id}/{instance_id}（先让它上报一次）",
        )

    prev = int(row.desired_limit or 0)
    new_limit = int(payload.limit)
    row.desired_limit = new_limit
    # Batch16：每次下发都落一行审计（append-only，不更新不删除）。
    # 和注册表分开：注册表只留最新值，审计表留住"改过几次、从几改到几、为什么"。
    db.add(WorkerQuotaAudit(
        worker_id=row.worker_id,
        instance_id=row.instance_id,
        desired_limit=new_limit,
        prev_limit=prev,
        command=payload.command,
        reason=payload.reason,
    ))
    db.commit()
    db.refresh(row)
    logger.info("并发配额下发：%s/%s desired=%s reason=%s",
                row.worker_id, row.instance_id, row.desired_limit, payload.reason)
    return CommandResult(
        trace_id=payload.trace_id,
        command=payload.command,
        worker_id=row.worker_id,
        instance_id=row.instance_id,
        desired_limit=row.desired_limit,
    )
