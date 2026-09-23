"""Engine, SessionLocal, get_session dependency (A2.9: one request = one
transaction, committed on success / rolled back on exception).

Defect 1 fix: every route MUST inject this as
`Depends(get_session, scope="function")`, never bare `Depends(get_session)`.
FastAPI's default dependency scope for a yield-dependency is "request", whose
exit code (this function's code after `yield`, i.e. the commit) runs on the
*outer* exit stack -- which FastAPI closes only after `await response(...)`
has already sent the response to the client (see
`fastapi.routing.request_response`: `function_astack` closes before the
response is awaited, `request_astack` closes after). With the default scope
a client that reads-after-write over a fresh connection can observe a state
that the write's own 200 response promised, before the commit that makes it
true has happened -- and a commit failure surfaces only after a 200 was
already on the wire. `scope="function"` puts the commit on `function_astack`,
which closes before the response is sent, restoring "the client's 200 means
the transaction is durable."."""
from collections.abc import Generator
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from rova.config import get_settings

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one DB transaction per request (A2.9)."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True)
class Scope:
    """A2.6 — the one place tenant isolation is enforced. Every tenant-scoped
    repository read/write takes a Scope and adds the WHERE clause."""
    kind: str  # 'platform' | 'pharmacy' | 'vendor'
    pharmacy_id: str | None = None
    vendor_id: str | None = None

    @staticmethod
    def platform() -> "Scope":
        return Scope(kind="platform")

    @staticmethod
    def pharmacy(pharmacy_id: str) -> "Scope":
        return Scope(kind="pharmacy", pharmacy_id=pharmacy_id)

    @staticmethod
    def vendor(vendor_id: str) -> "Scope":
        return Scope(kind="vendor", vendor_id=vendor_id)

    def owns_pharmacy(self, pharmacy_id: str) -> bool:
        return self.kind == "platform" or (self.kind == "pharmacy" and self.pharmacy_id == pharmacy_id)

    def owns_vendor(self, vendor_id: str) -> bool:
        return self.kind == "platform" or (self.kind == "vendor" and self.vendor_id == vendor_id)
