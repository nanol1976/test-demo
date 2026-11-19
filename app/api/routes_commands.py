"""运维指令接口（Batch4）。

只接受 3 条 master: 前缀指令，不做任何业务决策：
- master:pause        暂停该 trace（拦截后续派发）
- master:resume       恢复该 trace（并复位熔断）
- master:clear-breaker 仅复位熔断标志，不改暂停状态
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.logging import get_logger
from app.core.security import require_token
from app.repositories.ledger import LedgerRepository
from app.services.breaker import breaker

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])

ALLOWED_COMMANDS = {"master:pause", "master:resume", "master:clear-breaker"}


class CommandRequest(BaseModel):
    command: str = Field(..., description="master:pause | master:resume | master:clear-breaker")
    trace_id: str = Field(..., min_length=1, max_length=64)
    reason: Optional[str] = None


class CommandResult(BaseModel):
    trace_id: str
    command: str
    paused: bool
    breaker_open: bool


@router.post(
    "/commands",
    response_model=CommandResult,
    summary="下发运维指令（暂停 / 恢复 / 复位熔断）",
    description=(
        "只接受 master:pause、master:resume、master:clear-breaker 三条指令。"
        "暂停只会拦截该 trace 的后续派发，不改变任何任务的既有状态。"
    ),
)
def post_command(payload: CommandRequest, db: Session = Depends(get_db)) -> CommandResult:
    if payload.command not in ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"未知指令: {payload.command}")

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
