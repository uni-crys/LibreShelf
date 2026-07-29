from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.database import engine

router = APIRouter(tags=["Health"])


@router.get("/health")
def health():
    """Liveness probe: the API process can serve requests."""
    return {"status": "ok"}


@router.get("/ready")
def ready():
    """Readiness probe: the application can execute a SQLite query."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        ) from error
    return {"status": "ready", "checks": {"sqlite": "ok"}}
