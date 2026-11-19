"""Main FastAPI application for Master-Worker Platform."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health_router
from app.api import routes_messages
from app.bus import MessageBus
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动总线并注入路由表；退出时停机并记录残留报文数。"""
    bus = MessageBus(capacity=settings.bus_capacity, workers=settings.bus_workers)
    msg_router = routes_messages.build_message_router()
    bus.set_consumer(routes_messages._consume)
    routes_messages.configure(bus, msg_router)
    bus.start()
    app.state.bus = bus
    try:
        yield
    finally:
        leftover = bus.stop()
        logger.info("总线停机，残留未消费报文：%s 条", leftover)


app = FastAPI(
    title="Master-Worker Platform",
    description=(
        "Infrastructure platform for task orchestration "
        "(routing, queue, persistence, timeout, UI, auth)"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# 健康检查与报文接口
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(routes_messages.router, prefix="/api", tags=["messages"])
