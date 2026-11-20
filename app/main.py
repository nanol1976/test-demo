"""Main FastAPI application for Master-Worker Platform."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app.api import health_router
from app.api import routes_audit, routes_commands, routes_messages, routes_queries, routes_workers
from app.api.deps import get_engine, get_session_factory
from app.bus import MessageBus
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.models import Base
from app.services.scheduler import Scheduler

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


def ensure_schema(engine=None) -> list:
    """启动自检：缺表就按模型补建，并把缺失清单返回。

    为什么要有这一步：空库直接起服务时，第一个查询会抛 500 且被中间件吞成
    "Internal Server Error"，排查成本高。这里改成启动即明确告警。
    生产环境应改走迁移脚本（Batch6 补），自动建表只是兜底。
    engine 可传入（测试用），不传则取全局单例。
    """
    engine = engine if engine is not None else get_engine()
    existing = set(inspect(engine).get_table_names())
    missing = [t.name for t in Base.metadata.sorted_tables if t.name not in existing]
    if missing:
        try:
            Base.metadata.create_all(engine)
            logger.warning("启动自检：缺失表 %s，已按模型自动补建（生产请改用迁移脚本）", missing)
        except SQLAlchemyError as exc:
            # 多进程同时启动可能撞锁；此时不阻断启动，交由运维按日志处理
            logger.error("启动自检：自动建表失败（可能多进程并发启动撞锁）：%s", exc)
    return missing


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动总线与后台判定线程；退出时按序停机。"""
    app.state.missing_tables = ensure_schema()
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
app.include_router(routes_workers.router, tags=["workers"])
app.include_router(routes_audit.router, tags=["audit"])

# --- Batch5 极简前端：纯静态资源，不新增任何状态或接口 ---
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """台账看板首页。静态页不走 API 鉴权，页面内自行携带 X-API-Token。"""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
