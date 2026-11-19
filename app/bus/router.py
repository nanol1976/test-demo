"""报文路由（Batch2）。

职责严格限定：校验 → 查 handler → 调用。不做任何业务判断、不碰台账。
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Set

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.schemas import Envelope, MsgType, validate_envelope

logger = get_logger(__name__)

VALID_MSG_TYPES: Set[str] = {m.value for m in MsgType}


class UnknownMsgTypeError(Exception):
    """msg_type 不属于协议 v1.0 的 8 种。"""


class NoHandlerError(Exception):
    """协议合法但本平台未注册处理器。"""


@dataclass
class HandlerResult:
    archive_id: int
    status_changed: bool
    note: str = ""


Handler = Callable[[Session, Envelope], HandlerResult]


class Router:
    """handler 注册表 + 单条报文的路由入口。"""

    def __init__(self) -> None:
        self._handlers: Dict[MsgType, Handler] = {}

    def register(self, msg_type: Any, handler: Handler) -> None:
        key = msg_type if isinstance(msg_type, MsgType) else MsgType(msg_type)
        self._handlers[key] = handler

    def registered(self) -> Set[MsgType]:
        return set(self._handlers)

    def route(self, db: Session, raw: Dict[str, Any]) -> HandlerResult:
        msg_type = raw.get("msg_type")
        # 先判协议白名单：未知 msg_type 属于请求非法(400)，不是字段校验失败(422)
        if msg_type not in VALID_MSG_TYPES:
            raise UnknownMsgTypeError(f"未知 msg_type: {msg_type!r}")

        envelope = Envelope(**raw)
        envelope = validate_envelope(envelope)

        handler = self._handlers.get(envelope.msg_type)
        if handler is None:
            raise NoHandlerError(f"未注册 handler: {envelope.msg_type.value}")

        return handler(db, envelope)


__all__ = [
    "Router",
    "HandlerResult",
    "UnknownMsgTypeError",
    "NoHandlerError",
    "VALID_MSG_TYPES",
]
