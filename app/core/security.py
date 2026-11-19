"""最小鉴权占位（Batch3）。

规则：
- 配置 `API_TOKEN` 为空（默认）→ 完全不校验，本地开发零门槛；
- 配置了 `API_TOKEN` → 业务接口必须带 `X-API-Token`，常量时间比对；
- 健康检查永远放行（探针不能要令牌）。
"""
import hmac
from typing import Optional

from fastapi import Header, HTTPException

from app.core.config import get_settings

TOKEN_HEADER = "X-API-Token"


def require_token(x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER)) -> None:
    expected = get_settings().api_token
    if not expected:
        return
    if not x_api_token or not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Token")
