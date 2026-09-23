"""A15.4 rows 1-2."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova import __version__
from rova.core.db import get_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz(session: Session = Depends(get_session, scope="function")):
    session.execute(text("SELECT 1"))
    head = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {"status": "ok", "migration_head": head}
