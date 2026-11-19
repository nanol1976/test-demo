"""Health check routes for Master-Worker Platform."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
