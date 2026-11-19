"""API package for Master-Worker Platform."""
from app.api.routes_health import router as health_router

__all__ = ["health_router"]
