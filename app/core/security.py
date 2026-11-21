"""最小鉴权（Batch3 起，Batch28 加只读档，Batch31 加执行方档）。

规则：
- 三枚口令全为空（默认）→ 完全不校验，本地开发零门槛；
- 配了 `API_TOKEN` → 全权口令：读写皆可，常量时间比对；
- 另配了 `API_TOKEN_READONLY` → 只读档：能过鉴权，但任何写操作 403；
- 另配了 `API_TOKEN_WORKER` → 执行方档：只能注册/心跳与四类回写报文，管理指令 403；
- 健康检查永远放行（探针不能要令牌）；首页静态页不走鉴权（页面自己带口令）。

为什么要有执行方档（Batch31）：Batch28 的只读档把「能看」和「能动」拆开了，但漏了一个情形 ——
执行方本身必须能写（注册、心跳、回执），而注册接口 `POST /api/workers` 就是写操作，
于是质检岗拿只读口令永远上不了册。反过来把全权口令发给质检，就等于把「暂停派发、改并发、
优雅停止」的钥匙一起交出去，验收权形同虚设。
两把钥匙必须分开：执行方档能证明「我验完了」，但证明不了「我要停谁」。
"""
import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.core.config import get_settings

TOKEN_HEADER = "X-API-Token"
MODE_HEADER = "X-API-Mode"

ROLE_FULL = "full"
ROLE_READONLY = "readonly"
ROLE_WORKER = "worker"

# 执行方档允许投递的报文类型：全是 worker → master 的回写方向。
# 派单（task_dispatch）、人工确认、汇总属于管理方向，执行方档一律拒绝。
WORKER_MSG_TYPES = frozenset({
    "task_heartbeat",
    "task_return",
    "task_reject",
    "task_abort",
})


def resolve_role(token: Optional[str]) -> Optional[str]:
    """把一枚口令解析成权限档；None 表示「没有权限」。

    未配置任何口令时返回 full —— 这是本地开发的零门槛模式，保持 Batch3 的行为。
    """
    settings = get_settings()
    full = settings.api_token
    readonly = settings.api_token_readonly
    worker = settings.api_token_worker
    if not full and not readonly and not worker:
        return ROLE_FULL
    if not token:
        return None
    if full and hmac.compare_digest(token, full):
        return ROLE_FULL
    if readonly and hmac.compare_digest(token, readonly):
        return ROLE_READONLY
    if worker and hmac.compare_digest(token, worker):
        return ROLE_WORKER
    return None


def current_role(x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER)) -> str:
    """依赖：校验口令并返回本次请求的权限档（full / readonly / worker）。"""
    role = resolve_role(x_api_token)
    if role is None:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Token")
    return role


def require_token(x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER)) -> None:
    """依赖：只校验「口令对不对」，不看权限档（读接口用它）。"""
    current_role(x_api_token)


def require_write(role: str = Depends(current_role)) -> str:
    """依赖：管理写接口专用（下发指令）。只读档与执行方档到此为止，服务端直接 403。"""
    if role != ROLE_FULL:
        raise HTTPException(status_code=403, detail="只读口令不能写（下发指令请用全权口令）")
    return role


def require_worker_write(role: str = Depends(current_role)) -> str:
    """依赖：执行方回写专用（注册/心跳/回执）。全权或执行方可以，只读不行。

    报文类型的进一步收敛放在 `POST /api/messages` 里按 `role == ROLE_WORKER` 判定 ——
    同一个端点既收执行方回执也收管理派单，光靠依赖分不开方向。
    """
    if role not in (ROLE_FULL, ROLE_WORKER):
        raise HTTPException(
            status_code=403,
            detail="只读口令不能写（注册与回执请用执行方口令或全权口令）",
        )
    return role
