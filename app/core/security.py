"""最小鉴权（Batch3 起，Batch28 加只读档）。

规则：
- `API_TOKEN` 与 `API_TOKEN_READONLY` 都为空（默认）→ 完全不校验，本地开发零门槛；
- 配了 `API_TOKEN` → 全权口令：业务接口必须带 `X-API-Token`，常量时间比对；
- 另配了 `API_TOKEN_READONLY` → 这一枚也能过鉴权，但只能读（GET），任何写操作 403；
- 健康检查永远放行（探针不能要令牌）；首页静态页不走鉴权（页面自己带口令）。

为什么分两档：Batch27 把看板开到了局域网／Tailscale 网内，手机上也存了这枚口令。
一枚通吃意味着手机被看到一次就等于把「暂停流水线、改并发配额」的钥匙交出去。
只读档把「能看」和「能动」拆开，写操作在服务端拦，不靠前端不给按钮。
"""
import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.core.config import get_settings

TOKEN_HEADER = "X-API-Token"
MODE_HEADER = "X-API-Mode"

ROLE_FULL = "full"
ROLE_READONLY = "readonly"


def resolve_role(token: Optional[str]) -> Optional[str]:
    """把一枚口令解析成权限档；None 表示「没有权限」。

    未配置任何口令时返回 full —— 这是本地开发的零门槛模式，保持 Batch3 的行为。
    """
    settings = get_settings()
    full = settings.api_token
    readonly = settings.api_token_readonly
    if not full and not readonly:
        return ROLE_FULL
    if not token:
        return None
    if full and hmac.compare_digest(token, full):
        return ROLE_FULL
    if readonly and hmac.compare_digest(token, readonly):
        return ROLE_READONLY
    return None


def current_role(x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER)) -> str:
    """依赖：校验口令并返回本次请求的权限档（full / readonly）。"""
    role = resolve_role(x_api_token)
    if role is None:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Token")
    return role


def require_token(x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER)) -> None:
    """依赖：只校验「口令对不对」，不看权限档（读接口用它）。"""
    current_role(x_api_token)


def require_write(role: str = Depends(current_role)) -> str:
    """依赖：写接口专用。只读口令到此为止，服务端直接 403。"""
    if role != ROLE_FULL:
        raise HTTPException(status_code=403, detail="只读口令不能写（下发指令请用全权口令）")
    return role
