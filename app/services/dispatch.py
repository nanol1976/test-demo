"""状态迁移编排（Batch2）。

边界：
- 只按报文的 msg_type 做状态迁移与台账记录；
- 不做 DAG 拆解、不做验收判定、不做审批决策、不做超时控制；
- 所有写操作一律走 LedgerRepository，禁止在这里写 SQL 或自建 Session。
"""
import base64
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

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


def _resolve_target_instance(payload: Dict[str, Any]) -> Optional[str]:
    """Batch31-D：任务指定给哪个岗位。

    口径：`payload.target_instance`，形如「角色-主机」（如 qa-mac）。
    缺省/空串 = 不指定，身份族内先到先得（Batch9 以来一直如此）。
    只做格式校验，是否真实注册过由调用方查注册表。
    """
    raw = payload.get("target_instance")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise IllegalTransitionError(
            f"task_dispatch 被拒：payload.target_instance 必须是字符串（收到 {type(raw).__name__}）"
        )
    value = raw.strip()
    if not value:
        return None
    if len(value) > 64:
        raise IllegalTransitionError("task_dispatch 被拒：payload.target_instance 超过 64 字符")
    if not any(ch.isalnum() or ch in "-_." for ch in value):
        raise IllegalTransitionError(
            f"task_dispatch 被拒：payload.target_instance 只接受字母数字与 -_.（收到 {value!r}）"
        )
    return value


def _payload_json(payload: Dict[str, Any]) -> str:
    """载荷原文落库。平台只搬运不解释，故原样序列化（稳定排序便于比对）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _resolve_plugin(payload: Dict[str, Any]) -> Optional[str]:
    """Batch32-2：本单用哪个插件（plugins/<name>/ 的目录名）。

    只做格式校验：小写字母数字与 -_ ., ≤64。是否存在/是否过审/是否授权，
    由 app/services/plugins.py 判（索引可热改，不在报文层写死）。
    """
    raw = payload.get("plugin")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise IllegalTransitionError(
            f"task_dispatch 被拒：payload.plugin 必须是字符串（收到 {type(raw).__name__}）"
        )
    value = raw.strip()
    if not value:
        return None
    if len(value) > 64:
        raise IllegalTransitionError("task_dispatch 被拒：payload.plugin 超过 64 字符")
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", value):
        raise IllegalTransitionError(
            f"task_dispatch 被拒：payload.plugin 只接受小写字母数字与 ._-（收到 {value!r}）"
        )
    return value


def _gate_plugin(db: Session, repo: LedgerRepository, envelope: Envelope,
                 payload: Dict[str, Any], plugin_name: str,
                 target_worker: str, target_instance: Optional[str]) -> None:
    """带插件的派单：先判授权、再跑一级规则复核，把结论落审计，再决定放不放行。

    三道都必须过：
      1) 必须指定 target_instance（不指定岗位就没法判「这个岗位有没有资格」）；
      2) app/services/plugins.py 的三级判定必须 allow；
      3) 一级规则复核（plugins/<name>/probes.py）必须退出码 0。
    一次派单只落一行审计（最终结论），理由里带「授权命中哪一级 + 一级复核结果」。
    拒单不建任务、不建流水线，但审计行必须留下（append-only，自己 commit，
    不能因为随后抛异常被回滚吃掉）。
    """
    from app.services.plugins import judge_plugin, run_probes

    if not target_instance:
        repo.record_plugin_audit(
            trace_id=envelope.trace_id, task_id=envelope.task_id,
            plugin_name=plugin_name, plugin_version=None,
            worker_id=target_worker, instance_id="",
            decision="deny",
            reason="派单带 plugin 时必须指定 target_instance（不指定岗位无法判授权）",
        )
        raise IllegalTransitionError(
            f"task_dispatch 被拒：带 plugin（{plugin_name}）的派单必须指定 target_instance，"
            "否则无法判断这个岗位有没有插件权限"
        )

    verdict = judge_plugin(plugin_name, target_instance)
    decision = verdict["decision"]
    reason = verdict["reason"]

    if decision == "allow":
        probe = run_probes(plugin_name)
        if probe["ok"]:
            reason += "；一级复核通过" if not probe["skipped"] else "；" + probe["summary"]
        else:
            decision = "deny"
            reason += "；一级规则复核未通过：" + probe["summary"]

    repo.record_plugin_audit(
        trace_id=envelope.trace_id, task_id=envelope.task_id,
        plugin_name=plugin_name, plugin_version=verdict.get("version"),
        worker_id=target_worker, instance_id=target_instance,
        decision=decision, reason=reason,
    )
    if decision != "allow":
        raise IllegalTransitionError(
            f"task_dispatch 被拒：岗位 {target_instance} 不能用插件 {plugin_name}"
            f"（{reason}）"
        )


# Batch34：dsh（外部执行队）也要守规矩 —— 必须有岗位、有并发配额、留审计。
# dsh 单慢且贵，同实例上堆单只会互相拖死；默认一次只许 1 单在跑，MWP_DSH_MAX_INFLIGHT 可调。
DSH_MAX_INFLIGHT = 1


def dsh_max_inflight() -> int:
    """单实例同时允许多少个 dsh 单（默认 1；环境变量非法就当默认，且下限 1）。"""
    raw = os.environ.get("MWP_DSH_MAX_INFLIGHT")
    if not raw:
        return DSH_MAX_INFLIGHT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DSH_MAX_INFLIGHT
    return max(1, value)


def _resolve_adapter(payload: Dict[str, Any]) -> str:
    spec = payload.get("exec")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("adapter") or "").strip().lower()


def _gate_dsh(repo: LedgerRepository, envelope: Envelope, payload: Dict[str, Any],
              target_worker: str, target_instance: str) -> None:
    """dsh 单过闸：必须指定岗位 + 单实例并发配额。

    判定无论 allow 还是 deny 都写一行审计（plugin_audit，追加不删）：
    事后要能回答"这单为什么被拒/为什么放行"，而不是只有一句报错。
    """
    if _resolve_adapter(payload) != "dsh":
        return
    spec = payload.get("exec") if isinstance(payload.get("exec"), dict) else {}
    model = str(spec.get("model") or "").strip() or "（默认模型）"
    effort = str(spec.get("reasoning_effort") or "").strip() or "（不指定）"

    def audit(decision: str, reason: str) -> None:
        repo.record_plugin_audit(
            trace_id=envelope.trace_id, task_id=envelope.task_id,
            plugin_name="dsh", plugin_version="adapter",
            worker_id=target_worker, instance_id=target_instance,
            decision=decision, reason=reason,
        )

    if not target_instance:
        audit("deny", "dsh 单没指定 target_instance（配额与产物归属都无从计算）")
        raise IllegalTransitionError(
            "task_dispatch 被拒：exec.adapter=dsh 必须指定 target_instance（如 ops-win / ops-mac），"
            "否则不知道落在哪台机器，配额与产物归属都算不出来"
        )
    limit = dsh_max_inflight()
    inflight = repo.count_inflight(target_worker, target_instance)
    head = f"model={model} effort={effort} 实例={target_instance} 在跑 {inflight}/{limit}"
    if inflight >= limit:
        audit("deny", f"{head}：同实例 dsh 单已达配额上限（MWP_DSH_MAX_INFLIGHT={limit}）")
        raise IllegalTransitionError(
            f"task_dispatch 被拒：{head}，同实例 dsh 单已达上限"
            f"（MWP_DSH_MAX_INFLIGHT={limit}），等当前那单跑完再派"
        )
    audit("allow", head)


@_idempotent
def handle_task_dispatch(db: Session, envelope: Envelope) -> HandlerResult:
    payload: Dict[str, Any] = envelope.payload or {}
    # 03-权限矩阵：派发是 Master 的唯一权力，Worker 不得派活（越权先拦，连流水线都不建）
    if envelope.sender.value != "master":
        raise IllegalTransitionError(
            f"task_dispatch 越权：只有 Master 可以派发，报文来自 {envelope.sender.value}"
        )
    repo = LedgerRepository(db)

    # 先把「派给谁、用哪个插件」解出来：插件授权不过的单，连流水线都不建，
    # 只在 plugin_audit 留一行拒单痕迹（任务台账不占行）。
    target_worker = _resolve_target_worker(payload, envelope)
    target_instance = _resolve_target_instance(payload)
    plugin_name = _resolve_plugin(payload)
    if plugin_name:
        _gate_plugin(db, repo, envelope, payload, plugin_name, target_worker, target_instance)
    # Batch34：dsh 单在这里过配额闸（没有岗位、超出并发都当场拒，拒单理由写审计）
    _gate_dsh(repo, envelope, payload, target_worker, target_instance)

    pipeline = repo.get_pipeline(envelope.trace_id)
    if pipeline is None:
        pipeline = repo.create_pipeline(
            envelope.trace_id, payload.get("task_goal") or "（未提供 task_goal）"
        )

    _require_dispatch_gate(repo, pipeline, payload, envelope)

    if target_instance and not repo.worker_registered(target_worker, target_instance):
        # 派给从没注册过的岗位名 = 错字或岗位已下线，任务会永远挂空，必须当场拦下
        raise IllegalTransitionError(
            f"task_dispatch 被拒：岗位 {target_instance} 在注册表里从未出现"
            f"（worker_id={target_worker}），请先确认岗位名（形如「角色-主机」）"
        )

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
            target_instance=target_instance,
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
        target_instance=target_instance or "",
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


# ---------------------------------------------------------------- Batch32-1 异构复核

def _criteria_of(task) -> List[str]:
    """从任务的派发载荷里取验收标准（原样，不做业务解释）。"""
    try:
        data = json.loads(task.dispatch_payload or "{}")
    except (TypeError, ValueError):
        return []
    crit = data.get("acceptance_criteria")
    if not isinstance(crit, list):
        return []
    return [str(c) for c in crit if str(c).strip()]


def _criteria_ids(n: int) -> List[str]:
    return [f"c{i + 1}" for i in range(n)]


def _parse_review_verdict(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从复核回执里取逐项结论。

    两条来源，按可信度取：
      1) `payload.output.result` —— Worker 若把 stdout 解析成 JSON 会放这里；
      2) `payload.logs` —— 规则复核脚本固定只打一行 JSON，2000 字符尾截断也放得下。
    两处都取不到就返回 None，由调用方判 fail：没有逐项结论本身就不合格。
    """
    out = payload.get("output") or {}
    if isinstance(out, dict) and isinstance(out.get("result"), dict):
        return out["result"]
    logs = (payload.get("logs") or "").strip()
    if logs:
        try:
            data = json.loads(logs)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
    return None


def _evaluate(items, criteria: List[str]) -> tuple:
    """逐项对照标准，返回 (verdict, items_json, reason)。

    三条硬判据（设计稿 3.4）：
      ① 条数必须相等：少一条就说明标准没被逐条回答 → fail；
      ② 任一项 ok=false → fail，理由带该项编号；
      ③ 缺项理由必须点出编号（c3 之类），重派时才知道缺什么。
    """
    ids = _criteria_ids(len(criteria))
    if not isinstance(items, list):
        return "fail", None, "复核回执里没有逐项结论（items 缺失）"
    if len(items) != len(criteria):
        got = [str((it or {}).get("id") or "") if isinstance(it, dict) else "" for it in items]
        missing = [cid for cid in ids if cid not in got]
        extra = [g for g in got if g and g not in ids]
        reason = (
            f"逐项结论 {len(items)} 条 ≠ 验收标准 {len(criteria)} 条"
            + (f"；缺失项：{'、'.join(missing)}" if missing else "")
            + (f"；多出项：{'、'.join(extra)}" if extra else "")
        )
        return "fail", json.dumps(items, ensure_ascii=False), reason
    failed = []
    for cid, it in zip(ids, items):
        if not isinstance(it, dict):
            failed.append(f"{cid}(结论不是对象)")
            continue
        if not bool(it.get("ok")):
            why = str(it.get("reason") or "").strip()
            failed.append(f"{cid}：{why}" if why else f"{cid}：未通过")
    if failed:
        return "fail", json.dumps(items, ensure_ascii=False), "复核未通过 — " + "；".join(failed)
    return "pass", json.dumps(items, ensure_ascii=False), ""


def _spawn_review(db: Session, repo: LedgerRepository, task, payload: Dict[str, Any]) -> str:
    """有验收标准 → 派生一条复核单，派给「不是执行者」的在线实例。

    失败一律不阻断原单回传（原单照样落「待验收」），但结论如实写进 note 与台账字段，
    不做静默处理。返回一句说明，拼进回执 note。
    """
    # 标准在**派发载荷**里（不是回执载荷）：回执只说"我干完了"，标准得回原单上取
    criteria = _criteria_of(task)
    if not criteria:
        return ""
    criteria = [str(c) for c in criteria if str(c).strip()]
    if not criteria:
        return ""
    if getattr(task, "review_of", None):
        return ""                       # 复核单不再派生复核单（不递归）
    if getattr(task, "review_verdict", None):
        return "（已有复核结论，未重复派生）"

    review_id = f"{task.task_id}-rv"
    if repo.get_task(review_id) is not None:
        return "（复核单已存在，未重复派生）"

    done = getattr(task, "done_instance", None)
    artifacts = ((payload.get("output") or {}).get("artifacts")) or []
    if not isinstance(artifacts, list):
        artifacts = []
    # 证据只留路径与是否存在，不搬运大对象
    artifacts = [
        {"path": a.get("path"), "exists": bool(a.get("exists"))}
        for a in artifacts if isinstance(a, dict)
    ]

    requested = payload.get("reviewer")
    if not isinstance(requested, str) or not requested.strip():
        # 回执报文按 schema 收敛，顶层自定义字段会被丢掉；metadata 是透传字典，放这里
        meta = payload.get("metadata") or {}
        requested = meta.get("reviewer") if isinstance(meta, dict) else None
    reviewer = None
    if isinstance(requested, str) and requested.strip():
        requested = requested.strip()
        if done and requested == done:
            # 复核者 = 执行者本人 → 不建单，标 unreviewed（防作弊第 1 条）
            repo.set_review_verdict(task.task_id, "unreviewed")
            logger.warning("review 复核者与执行者同实例 task=%s instance=%s",
                           task.task_id, requested)
            return f"（复核者与执行者同为 {requested}，未建复核单，任务标 unreviewed）"
        if repo.worker_registered(task.target_worker, requested):
            reviewer = (task.target_worker, requested)
    if reviewer is None:
        reviewer = repo.pick_reviewer(done)

    if reviewer is None:
        repo.set_review_verdict(task.task_id, "unreviewed")
        return "（无合格复核者：需在线且不是执行者本人，任务标 unreviewed；签收需 force_accept）"

    ctx = {
        "review_of": task.task_id,
        "criteria": criteria,
        "artifacts": artifacts,
        "self_check": ((payload.get("metadata") or {}).get("self_check") or ""),
    }
    ctx_json = json.dumps(ctx, ensure_ascii=False, sort_keys=True, default=str)
    b64 = base64.urlsafe_b64encode(ctx_json.encode("utf-8")).decode("ascii")

    review_payload = {
        "task_goal": f"复核 {task.task_id}：逐条核对 {len(criteria)} 条验收标准",
        "target_worker": task.target_worker,
        "target_instance": reviewer[1],
        "max_retry": 1,
        # 复核单自身不带标准：否则会再派生一层复核，评审链无限延长
        "acceptance_criteria": [],
        "review_context": ctx,
        "exec": {
            # 规则复核：纯标准库、秒级、离线可判。判的是「标准有没有被逐条答满」，
            # 不是「活干得好不好」——后者归人签收。
            "script": "scripts/task_review.py",
            "args": ["--review-b64", b64],
            "timeout_seconds": 120,
        },
    }
    repo.create_task(
        trace_id=task.trace_id,
        task_id=review_id,
        target_worker=task.target_worker,
        status="执行中",
        max_retry=1,
        target_instance=reviewer[1],
        review_of=task.task_id,
    )
    repo.record_dispatch(
        review_id,
        datetime.now().astimezone(),
        payload_json=_payload_json(review_payload),
        target_worker=task.target_worker,
        target_instance=reviewer[1],
    )
    logger.info("review 派生 %s -> %s（原单 %s）", review_id, reviewer[1], task.task_id)
    return f"（已派生复核单 {review_id}，复核者 {reviewer[1]}）"


def _finish_review(db: Session, repo: LedgerRepository, review_task,
                   envelope: Envelope, archive_id: int, payload: Dict[str, Any]) -> HandlerResult:
    """复核单回执：写结论 → pass 留「待验收」/ fail 驳回原单。"""
    original = repo.get_task(review_task.review_of)
    if original is None:
        repo.update_task_status(review_task.task_id, "验收通过")
        return HandlerResult(archive_id, True, "复核单已回传，但被复核任务不存在（结论无处可落）")
    if getattr(original, "review_verdict", None):
        # 结论只写一次（防作弊第 4 条）：二次回执一律拒绝，不动任何状态
        raise IllegalTransitionError(
            f"复核结论已写入（{original.review_verdict}），不允许改写：{original.task_id}"
        )

    criteria = _criteria_of(original)
    verdict = _parse_review_verdict(payload)
    items = (verdict or {}).get("items") if isinstance(verdict, dict) else None
    result, items_json, reason = _evaluate(items, criteria)

    repo.set_review_verdict(original.task_id, result, items_json)
    repo.update_task_status(review_task.task_id, "验收通过")

    if result == "pass":
        logger.info("review pass task=%s reviewer=%s",
                    original.task_id, review_task.done_instance)
        return HandlerResult(
            archive_id, True,
            f"复核通过（{len(criteria)} 条标准逐项答满）；原单 {original.task_id} 保持「待验收」，等签收",
        )

    repo.record_reject(original.task_id, reason[:1000])
    remaining = repo.get_task(original.task_id).remaining_retry
    if remaining <= 0:
        repo.update_task_status(original.task_id, "执行失败")
        return HandlerResult(
            archive_id, True,
            f"复核未通过：{reason[:200]}；重试已耗尽，原单转「执行失败」",
        )
    repo.update_task_status(original.task_id, "验收驳回")
    return HandlerResult(
        archive_id, True,
        f"复核未通过：{reason[:200]}；原单 {original.task_id} 转「验收驳回」，剩余重试 {remaining}",
    )


@_idempotent
def handle_task_return(db: Session, envelope: Envelope) -> HandlerResult:
    repo = LedgerRepository(db)
    task = _require_task(repo, envelope.task_id, "task_return")
    _require_same_trace(task, envelope, "task_return")
    _require_owner(task, envelope, "task_return")
    # 复核结论只写一次：复核单已「验收通过」、原单也已有结论时，先报"不许改写"，
    # 别让人以为是"状态不对"（两种错因要能一眼分清）。
    if getattr(task, "review_of", None):
        original = repo.get_task(task.review_of)
        if original is not None and getattr(original, "review_verdict", None):
            raise IllegalTransitionError(
                f"复核结论已写入（{original.review_verdict}），不允许改写：{original.task_id}"
            )
    _require_status(task, TASK_RETURN_FROM, "task_return")

    payload: Dict[str, Any] = envelope.payload or {}
    archive_id = archive_message(db, envelope)
    repo.mark_return_time(envelope.task_id, envelope.timestamp)
    # Batch31-D：回执带来 instance_id 就落「实际执行岗位」；老 Worker 不带则留空（不阻断）
    meta = payload.get("metadata") or {}
    done_instance = meta.get("instance_id") if isinstance(meta, dict) else None

    # Batch32-1：复核单回执不落「待验收」，先把结论写给被复核任务
    if getattr(task, "review_of", None):
        if isinstance(done_instance, str) and done_instance.strip():
            repo.mark_done_instance(envelope.task_id, done_instance.strip())
        return _finish_review(db, repo, task, envelope, archive_id, payload)

    if isinstance(done_instance, str) and done_instance.strip():
        repo.mark_done_instance(envelope.task_id, done_instance.strip())
    repo.update_task_status(envelope.task_id, "待验收")
    note = "任务已回传，状态：待验收"
    if task.target_instance and done_instance and done_instance.strip() != task.target_instance:
        # 指定了岗位却被别人干了：不改状态（成果真实有效），但在回执里当场点出来
        note += f"（注意：实际执行岗位 {done_instance.strip()} ≠ 指定岗位 {task.target_instance}）"
        logger.warning(
            "task_return 岗位不符 task=%s 指定=%s 实际=%s",
            envelope.task_id, task.target_instance, done_instance.strip(),
        )
    # 有验收标准就派生复核单：验收从「人拍板」变成「规则复核 + 人签收」
    db.refresh(task)
    note += _spawn_review(db, repo, task, payload)
    return HandlerResult(archive_id, True, note)


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
    """人工确认答复（Batch31 起承载「验收通过」）。

    原先只留痕、不推进状态 —— 等于确认完了什么都不发生，于是任务永远停在「待验收」，
    六步 SOP 的第 5 步（验收）实际是空的：状态枚举里有「验收通过」，但没有任何报文能写它。
    这里不新增报文类型（8 种是对外契约，加一种就是接口面扩容），只让既有报文说完整话：
    payload 里显式写 `accept_tasks=true` 才推进，其余情形仍只留痕。
    """
    payload = envelope.payload or {}
    archive_id = archive_message(db, envelope)
    if not payload.get("accept_tasks"):
        return HandlerResult(archive_id, False, "人工确认答复已记录（仅留痕，不推进状态）")

    repo = LedgerRepository(db)
    if repo.get_pipeline(envelope.trace_id) is None:
        raise TaskNotFoundError(f"human_confirm_resp 失败：流水线不存在 {envelope.trace_id}")
    pending = repo.count_tasks_by_status_in_trace(envelope.trace_id, "待验收")
    # Batch32-1：签收闸门——unreviewed 的单要显式 force_accept 才推
    accepted = repo.accept_pending_tasks(
        envelope.trace_id, force_accept=bool(payload.get("force_accept"))
    )
    note = f"验收通过 {len(accepted)} 个任务" + (
        "：" + "、".join(accepted) if accepted else "（没有可签收的任务）")
    blocked = pending - len(accepted)
    if blocked > 0:
        note += f"；另有 {blocked} 个「待验收」被复核闸门拦下（unreviewed，需 force_accept=true）"
    return HandlerResult(archive_id, bool(accepted), note)


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


