"""Main FastAPI application for Master-Worker Platform."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health_router
from app.api import routes_commands, routes_messages, routes_queries
from app.api.deps import get_session_factory
from app.bus import MessageBus
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.services.scheduler import Scheduler

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动总线与后台判定线程；退出时按序停机。"""
    bus = MessageBus(capacity=settings.bus_capacity, workers=settings.bus_workers)
    msg_router = routes_messages.build_message_router()
    bus.set_consumer(routes_messages._consume)
    routes_messages.configure(bus, msg_router)
    bus.start()
    app.state.bus = bus

    scheduler = Scheduler(get_session_factory())
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.stop()
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

# 健康检查 / 报文 / 台账 / 运维指令
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(routes_messages.router, prefix="/api", tags=["messages"])
app.include_router(routes_queries.router, prefix="/api", tags=["ledger"])
app.include_router(routes_commands.router, prefix="/api", tags=["commands"])
