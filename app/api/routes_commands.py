"""运维指令接口（Batch4；Batch15 增加实例级并发配额下发）。

只接受 master: 前缀指令，不做任何业务决策：
- master:pause          暂停该 trace（拦截后续派发）
- master:resume         恢复该 trace（并复位熔断）
- master:clear-breaker  仅复位熔断标志，不改暂停状态
- master:worker-limit   给某个执行方实例下发期望并发上限（Batch15）
- master:stop           落「优雅停止」标记，worker 收尾退出（Batch29；master:resume 会清标记）

Batch18：四条指令**全部**落审计（`worker_quota_audit`，append-only）。
暂停/恢复/复位熔断这三条是流水线级的，没有"实例"这个概念，审计里用
`worker_id='master'`、`instance_id='trace:<trace_id>'` 表示目标，`desired_limit` 记 0。

Batch22：审计新增"操作者"列。来源是请求头 X-Operator（缺省为空），
平台自身没有身份体系，所以只记"调用方自报的名字"，不当作可信身份——这一点写进文档，
免得以后有人拿它当权限依据。

Batch15 的动机：Worker 自报的 concurrency 只是"我现在开几个"，平台没法要求它开几个。
弹性并发的上限如果只能靠改命令行重启进程，就没法在运行中收放。这里复用同一条指令通道
（不新增路径、方法仍是 POST），让平台把 desired_limit 写进注册表，Worker 在下一次
注册表上报的响应里读到并立即生效。limit=0 表示撤销下发、恢复 Worker 自己的配置。
"""
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.logging import get_logger
from app.core.security import require_token, require_write
from app.core.stopflag import clear_stop, request_stop
from app.models import WorkerQuotaAudit, WorkerRegistry
from app.repositories.ledger import LedgerRepository
from app.services.breaker import breaker

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_token)])

ALLOWED_COMMANDS = {"master:pause", "master:resume", "master:clear-breaker",
                    "master:worker-limit", "master:stop"}


class CommandRequest(BaseModel):
    command: str = Field(..., description="master:pause | master:resume | master:clear-breaker | master:worker-limit | master:stop")
    trace_id: Optional[str] = Field(default=None, max_length=64,
                                    description="流水线指令必填；master:worker-limit / master:stop 可不填")
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
    # Batch29：是否已落下"优雅停止"标记
    stopping: bool = False


@router.post(
    "/commands",
    response_model=CommandResult,
    summary="下发运维指令（暂停 / 恢复 / 复位熔断 / 实例并发配额 / 优雅停止）",
    description=(
        "只接受 master:pause、master:resume、master:clear-breaker、master:worker-limit、master:stop 五条指令。"
        "暂停只会拦截该 trace 的后续派发，不改变任何任务的既有状态。"
        "master:worker-limit 只写注册表的 desired_limit，由 Worker 下次上报时自行生效，"
        "平台不会去杀在飞的任务。"
        "master:stop 落一个停止标记文件（worker 下一轮轮询到即收尾退出），master:resume 会把它清掉。"
    ),
)
def post_command(payload: CommandRequest, db: Session = Depends(get_db),
                 operator: Optional[str] = Header(default=None, alias="X-Operator"),
                 role: str = Depends(require_write)) -> CommandResult:
    if payload.command not in ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"未知指令: {payload.command}")

    if payload.command == "master:worker-limit":
        return _worker_limit(payload, db, operator)

    # Batch29：优雅停止——落一个标记文件，worker 下一轮轮询到就收尾退出。
    # 不需要 trace_id（这是整机级动作，不是某条流水线），也不改任何任务状态。
    if payload.command == "master:stop":
        path = request_stop(payload.reason or "")
        _audit(db, command=payload.command, worker_id="master",
               instance_id="data", reason=payload.reason, operator=operator)
        db.commit()
        logger.info("优雅停止标记已落盘：%s reason=%s", path, payload.reason)
        return CommandResult(command=payload.command, stopping=True)

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
        # Batch29：resume 顺手把「优雅停止」标记清掉（幂等，本来没有也不报错），
        # 这样一条 resume 就能同时恢复"停机"和"暂停派发"。
        clear_stop()
    else:
        breaker.reset(payload.trace_id)

    # Batch18：流水线级指令也留痕（同一张审计表，不改结构）
    _audit(db, command=payload.command, worker_id="master",
           instance_id=f"trace:{payload.trace_id}", reason=payload.reason,
           operator=operator)
    db.commit()
    logger.info(
        "运维指令：%s trace=%s reason=%s", payload.command, payload.trace_id, payload.reason
    )
    return CommandResult(
        trace_id=payload.trace_id,
        command=payload.command,
        paused=bool(pipeline.pause_flag),
        breaker_open=breaker.is_open(payload.trace_id),
    )


def _worker_limit(payload: CommandRequest, db: Session, operator: Optional[str] = None) -> CommandResult:
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
    # Batch16/18：每次下发都落一行审计（append-only，不更新不删除）。
    # 和注册表分开：注册表只留最新值，审计表留住"改过几次、从几改到几、为什么"。
    _audit(db, command=payload.command, worker_id=row.worker_id,
           instance_id=row.instance_id, desired_limit=new_limit, prev_limit=prev,
           reason=payload.reason, operator=operator)
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


def _audit(db: Session, command: str, worker_id: str, instance_id: str,
           desired_limit: int = 0, prev_limit: int = 0,
           reason: Optional[str] = None,
           operator: Optional[str] = None) -> None:
    """审计落一行。四条指令共用，`command` 列区分是哪一条。

    operator 来自 X-Operator 头，只当"调用方自报的名字"记录，不做鉴权依据。

    HTTP 头只能放 latin-1，中文名字必须由调用方先 percent-encode（浏览器用
    encodeURIComponent），这里 unquote 还原；纯 ASCII 名字 unquote 后不变。
    """
    db.add(WorkerQuotaAudit(
        worker_id=worker_id,
        instance_id=instance_id,
        desired_limit=desired_limit,
        prev_limit=prev_limit,
        command=command,
        reason=reason,
        operator=unquote(operator or "")[:64],
    ))
