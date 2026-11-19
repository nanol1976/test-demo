"""状态迁移编排（Batch2）。

边界：
- 只按报文的 msg_type 做状态迁移与台账记录；
- 不做 DAG 拆解、不做验收判定、不做审批决策、不做超时控制；
- 所有写操作一律走 LedgerRepository，禁止在这里写 SQL 或自建 Session。
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bus.router import HandlerResult, Router
from app.core.logging import get_logger
from app.models import MessageArchive
from app.repositories.ledger import PIPELINE_STATUSES, LedgerRepository
from app.schemas import Envelope, MsgType, Sender
from app.services import ArchiveService

logger = get_logger(__name__)

# 迁移白名单：msg_type -> 允许的源状态
TASK_RETURN_FROM = {"执行中"}
TASK_REJECT_FROM = {"执行中", "待验收"}  # 05 文档：执行中/待验收 均可驳回
# 05 文档：验收驳回后 Master 可重派（remaining_retry 已在驳回时扣减，重派不再重复扣）
TASK_DISPATCH_FROM = {"待调度下发", "验收驳回"}


class IllegalTransitionError(Exception):
    """当前状态不接受该报文（409）。"""


class TaskNotFoundError(Exception):
    """报文中引用的任务不存在（404）。"""


# ---------------------------------------------------------------- 归档（幂等）

def archive_message(db: Session, envelope: Envelope) -> int:
    """写归档并返回 archive_id。

    顺序固定：先写 JSONL，再写索引行。
    同一 (trace_id, task_id, msg_type, timestamp) 重复投递按幂等处理：
    返回既有 archive_id，不再追加 JSONL、不报错。
    """
    existing = _find_archive(db, envelope)
    if existing is not None:
        return existing.id

    service = ArchiveService(db)
    try:
        record = service.append_archive(envelope.trace_id, envelope.task_id, envelope)
        return record.id
    except IntegrityError:
        # 并发下同一条报文撞了唯一约束：按幂等处理，不回滚已落盘的 JSONL
        db.rollback()
        row = _find_archive(db, envelope)
        if row is None:
            raise
        return row.id


def _find_archive(db: Session, envelope: Envelope) -> Optional[MessageArchive]:
    stmt = select(MessageArchive).where(
        MessageArchive.trace_id == envelope.trace_id,
        MessageArchive.task_id == envelope.task_id,
        MessageArchive.msg_type == envelope.msg_type.value,
        MessageArchive.timestamp == envelope.timestamp,
    )
    return db.execute(stmt).scalars().first()


# ---------------------------------------------------------------- 幂等前置

# 只有"执行方动作"才做内容级重放拦截。
# task_dispatch 刻意排除：驳回后 Master 用同样 payload 重派是合法重试，
# 若按内容判重会被误杀（同一 timestamp 的精确重放仍由幂等键拦住）。
# Batch12 修正：心跳是"周期性合法重复"，不能按内容去重。
# 一个跑 10 分钟的任务会发上百条内容完全一样的心跳，若按 sender+payload 拦成重放，
# 台账的 last_heartbeat_time 会永远停在第一条——"还在干活"的证据就丢了。
# 心跳的重复投递交给幂等键（trace_id+task_id+msg_type+timestamp）兜底。
REPLAY_GUARDED_TYPES = {"task_return", "task_reject", "task_abort"}


def _fingerprint(envelope: Envelope):
    """重放指纹：发送方 + payload 规范化 JSON。

    攻击者改 timestamp 重放同一条报文，幂等键会失效（timestamp 变了），
    但 sender 与 payload 一模一样——指纹一致即判为重放，不推进状态、不扣次数。
    """
    return (
        envelope.sender.value,
        json.dumps(envelope.payload or {}, sort_keys=True, ensure_ascii=False, default=str),
    )


def _find_replay(db: Session, envelope: Envelope) -> Optional[MessageArchive]:
    stmt = select(MessageArchive).where(
        MessageArchive.trace_id == envelope.trace_id,
        MessageArchive.task_id == envelope.task_id,
        MessageArchive.msg_type == envelope.msg_type.value,
    )
    want = _fingerprint(envelope)
    for row in db.execute(stmt).scalars().all():
        try:
            data = json.loads(row.raw_json)
        except (TypeError, ValueError):
            continue
        got = (
            data.get("sender"),
            json.dumps(data.get("payload") or {}, sort_keys=True, ensure_ascii=False, default=str),
        )
        if got == want:
            return row
    return None


def _idempotent(func):
    """幂等 + 重放拦截，两者都必须在状态校验之前。

    1. 幂等键 `(trace_id, task_id, msg_type, timestamp)` 命中 → 直接返回同一 archive_id；
    2. 键没命中但 sender + payload 与历史报文完全一致（换了 timestamp 的重放）→ 同样拦截。
    顺序很重要：若先做状态校验，重复投递会被误判成非法迁移（409）。
    """

    def wrapper(db: Session, envelope: Envelope) -> HandlerResult:
        existing = _find_archive(db, envelope)
        if existing is not None:
            return HandlerResult(existing.id, False, "重复报文，已忽略（幂等）")
        if envelope.msg_type.value in REPLAY_GUARDED_TYPES:
            replay = _find_replay(db, envelope)
            if replay is not None:
                return HandlerResult(replay.id, False, "重复报文（内容一致），已忽略（重放拦截）")
        return func(db, envelope)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# ---------------------------------------------------------------- 迁移校验

def _require_task(repo: LedgerRepository, task_id: str, action: str) -> Any:
    task = repo.get_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"{action} 失败：任务不存在 {task_id}")
    return task


def _require_owner(task: Any, envelope: Envelope, action: str, allow_master: bool = False) -> None:
    """越权校验：执行方动作只能由任务归属的执行方发出。

    03-权限矩阵：心跳 / 回传 / 驳回 / 中止 统统是「Worker 自己的任务」，
    Master 唯一例外是 task_abort（叫停权，04 协议明确 master/worker 都可发）。
    伪造他人 task_id 发报文 → 409，且不落任何状态。
    """
    who = envelope.sender.value
    if allow_master and who == "master":
        return
    if who != task.target_worker:
        extra = "（Master 可叫停，但 Worker 只能中止自己的任务）" if allow_master else ""
        raise IllegalTransitionError(
            f"{action} 越权：任务 {task.task_id} 归属 {task.target_worker}，报文来自 {who}{extra}"
        )


def _require_status(task: Any, allowed: set, action: str) -> None:
    if task.status not in allowed:
        raise IllegalTransitionError(
            f"{action} 非法迁移：当前状态「{task.status}」，仅接受 {sorted(allowed)}"
        )


def _require_same_trace(task: Any, envelope: Envelope, action: str) -> None:
    """报文里的 trace_id 必须与任务已归属的 trace 一致，否则拒绝。"""
    if task.trace_id != envelope.trace_id:
        raise IllegalTransitionError(
            f"{action} 非法：任务 {task.task_id} 属于 trace {task.trace_id}，"
            f"报文 trace 为 {envelope.trace_id}"
        )


# ---------------------------------------------------------------- 各 msg_type

def _require_dispatch_gate(
    repo: LedgerRepository, pipeline, payload: Dict[str, Any], envelope: Envelope
) -> None:
    """派发前的三个闸门：暂停 → 熔断 → 依赖未满足。任一不过都拒绝，且不落状态。"""
    from app.services.breaker import breaker

    if pipeline is not None and pipeline.pause_flag:
        raise IllegalTransitionError(
            f"task_dispatch 被拒：流水线 {pipeline.trace_id} 处于暂停状态，需先 resume"
        )
    if breaker.is_open(envelope.trace_id):
        raise IllegalTransitionError(
            f"task_dispatch 被拒：流水线 {envelope.trace_id} 已熔断（{breaker.reason(envelope.trace_id)}）"
        )
    deps = payload.get("depends_on") or []
    unmet = []
    for dep_id in deps:
        dep = repo.get_task(dep_id)
        if dep is None or dep.status != "验收通过":
            unmet.append(f"{dep_id}({dep.status if dep else '不存在'})")
    if unmet:
        raise IllegalTransitionError(f"task_dispatch 被拒：依赖未满足 {unmet}")


def _resolve_target_worker(payload: Dict[str, Any], envelope: Envelope) -> str:
    """任务归属哪个执行方。

    口径（Batch9 修正）：协议文档示例写的是 `payload.target_worker`，
    旧实现却取派发报文的 `sender`——Master 派给 workbuddy 的任务会被记成 master，
    Worker 按自己名字轮询永远查不到。现改为：
      1) 优先 `payload.target_worker`；
      2) 缺省回退 `sender`（兼容既有不带该字段的报文）；
      3) 取值必须属于 5 值白名单，否则拒绝（不落状态）。
    """
    raw = payload.get("target_worker")
    allowed = {s.value for s in Sender}
    if raw is None or raw == "":
        return envelope.sender.value
    if not isinstance(raw, str) or raw not in allowed:
        raise IllegalTransitionError(
            f"task_dispatch 被拒：payload.target_worker 不合法（{raw!r}），只接受 {sorted(allowed)}"
        )
    return raw


def _payload_json(payload: Dict[str, Any]) -> str:
    """载荷原文落库。平台只搬运不解释，故原样序列化（稳定排序便于比对）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


@_idempotent
def handle_task_dispatch(db: Session, envelope: Envelope) -> HandlerResult:
    payload: Dict[str, Any] = envelope.payload or {}
    # 03-权限矩阵：派发是 Master 的唯一权力，Worker 不得派活（越权先拦，连流水线都不建）
    if envelope.sender.value != "master":
        raise IllegalTransitionError(
            f"task_dispatch 越权：只有 Master 可以派发，报文来自 {envelope.sender.value}"
        )
    repo = LedgerRepository(db)

    pipeline = repo.get_pipeline(envelope.trace_id)
    if pipeline is None:
        pipeline = repo.create_pipeline(
            envelope.trace_id, payload.get("task_goal") or "（未提供 task_goal）"
        )

    _require_dispatch_gate(repo, pipeline, payload, envelope)

    target_worker = _resolve_target_worker(payload, envelope)

    task = repo.get_task(envelope.task_id)
    if task is None:
        # 首次派发：按报文建任务，落到「待调度下发」后再转执行中
        repo.create_task(
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            target_worker=target_worker,
            depends_on=payload.get("depends_on"),
            max_retry=int(payload.get("max_retry", 3)),
            is_high_risk=bool(payload.get("is_high_risk", False)),
            status="待调度下发",
        )
        task = repo.get_task(envelope.task_id)
    else:
        _require_status(task, TASK_DISPATCH_FROM, "task_dispatch")
        if task.status == "验收驳回" and task.remaining_retry <= 0:
            raise IllegalTransitionError(
                f"task_dispatch 被拒：任务 {task.task_id} 重试次数已耗尽（remaining_retry=0），"
                "按 05 状态机应转「执行失败」，需新建任务重来"
            )

    if task.trace_id != envelope.trace_id:
        raise IllegalTransitionError(
            f"task_dispatch 非法：任务 {task.task_id} 已属于 trace {task.trace_id}"
        )

    archive_id = archive_message(db, envelope)
    # dispatch_time + 载荷原文 + 执行方归属，一次落账（只更新，不删除）
    repo.record_dispatch(
        envelope.task_id,
        envelope.timestamp,
        payload_json=_payload_json(payload),
        target_worker=target_worker,
    )
    repo.update_task_status(envelope.task_id, "执行中")
    return HandlerResult(archive_id, True, "任务已下发，状态：执行中")


@_idempotent
def handle_task_heartbeat(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    task = _require_task(repo, envelope.task_id, "task_heartbeat")
    _require_same_trace(task, envelope, "task_heartbeat")
    _require_owner(task, envelope, "task_heartbeat")
    archive_id = archive_message(db, envelope)
    # Batch12：台账记最近心跳，供"执行方在线视图"聚合（仍不改变任务状态）
    repo.mark_heartbeat(envelope.task_id, envelope.timestamp)
    return HandlerResult(archive_id, False, "心跳已记录（不改状态，超时判定属 Batch4）")


@_idempotent
def handle_task_return(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    task = _require_task(repo, envelope.task_id, "task_return")
    _require_same_trace(task, envelope, "task_return")
    _require_owner(task, envelope, "task_return")
    _require_status(task, TASK_RETURN_FROM, "task_return")

    archive_id = archive_message(db, envelope)
    repo.mark_return_time(envelope.task_id, envelope.timestamp)
    repo.update_task_status(envelope.task_id, "待验收")
    return HandlerResult(archive_id, True, "任务已回传，状态：待验收")


@_idempotent
def handle_task_reject(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    task = _require_task(repo, envelope.task_id, "task_reject")
    _require_same_trace(task, envelope, "task_reject")
    _require_owner(task, envelope, "task_reject")
    _require_status(task, TASK_REJECT_FROM, "task_reject")

    reason = (envelope.payload or {}).get("reason") or "（未提供驳回原因）"
    archive_id = archive_message(db, envelope)
    repo.record_reject(envelope.task_id, reason)
    remaining = repo.get_task(envelope.task_id).remaining_retry
    if remaining <= 0:
        # 05 状态机：验收驳回 → 执行失败（重试次数耗尽）
        repo.update_task_status(envelope.task_id, "执行失败")
        return HandlerResult(
            archive_id, True, f"已驳回且重试次数耗尽（remaining_retry=0），状态转「执行失败」"
        )
    repo.update_task_status(envelope.task_id, "验收驳回")
    return HandlerResult(
        archive_id, True, f"已驳回并扣减重试次数，剩余重试 {remaining} 次（重派由 Master 决定）"
    )


@_idempotent
def handle_task_abort(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    task = _require_task(repo, envelope.task_id, "task_abort")
    _require_same_trace(task, envelope, "task_abort")
    _require_owner(task, envelope, "task_abort", allow_master=True)

    reason = (envelope.payload or {}).get("reason") or "（未提供中止原因）"
    archive_id = archive_message(db, envelope)
    repo.record_abort(envelope.task_id, reason)
    repo.update_task_status(envelope.task_id, "执行失败")
    repo.update_pipeline_status(envelope.trace_id, "aborted")
    return HandlerResult(archive_id, True, "任务已中止，状态：执行失败；流水线：aborted")


@_idempotent
def handle_human_confirm_req(db: Session, envelope: Envelope) -> HandlerResult:
    # 只留痕：高危卡点的审批决策属 Master，平台不做判断
    archive_id = archive_message(db, envelope)
    return HandlerResult(archive_id, False, "人工确认请求已记录（仅留痕，不推进状态）")


@_idempotent
def handle_human_confirm_resp(db: Session, envelope: Envelope) -> HandlerResult:
    archive_id = archive_message(db, envelope)
    return HandlerResult(archive_id, False, "人工确认答复已记录（仅留痕，不推进状态）")


@_idempotent
def handle_task_summary(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    pipeline = repo.get_pipeline(envelope.trace_id)
    if pipeline is None:
        raise TaskNotFoundError(f"task_summary 失败：流水线不存在 {envelope.trace_id}")

    target_status = (envelope.payload or {}).get("pipeline_status")
    if target_status not in PIPELINE_STATUSES:
        raise IllegalTransitionError(f"task_summary 非法流水线状态：{target_status}")

    archive_id = archive_message(db, envelope)
    repo.update_pipeline_status(
        envelope.trace_id, target_status, end_time=envelope.timestamp
    )
    return HandlerResult(archive_id, True, f"流水线状态已更新为 {target_status}")


HANDLERS = {
    MsgType.TASK_DISPATCH: handle_task_dispatch,
    MsgType.TASK_HEARTBEAT: handle_task_heartbeat,
    MsgType.TASK_RETURN: handle_task_return,
    MsgType.TASK_REJECT: handle_task_reject,
    MsgType.TASK_ABORT: handle_task_abort,
    MsgType.HUMAN_CONFIRM_REQ: handle_human_confirm_req,
    MsgType.HUMAN_CONFIRM_RESP: handle_human_confirm_resp,
    MsgType.TASK_SUMMARY: handle_task_summary,
}


def register_all(router: Router) -> None:
    """把 8 种 msg_type 全部挂上处理器。少一种都算本批未完成。"""
    for msg_type, handler in HANDLERS.items():
        router.register(msg_type, handler)


