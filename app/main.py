"""Main FastAPI application for Master-Worker Platform."""
from fastapi import FastAPI
from app.core.logging import setup_logging
from app.api import health_router
from app.core.config import get_settings


# Initialize logging
settings = get_settings()
setup_logging(settings.log_level)

# Create FastAPI app
app = FastAPI(
    title="Master-Worker Platform",
    description="Infrastructure platform for task orchestration (routing, queue, persistence, timeout, UI, auth)",
    version="0.1.0",
)

# Register health check routes
app.include_router(health_router, prefix="/api", tags=["health"])
