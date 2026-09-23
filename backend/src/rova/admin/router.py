"""A14.6 endpoint 57: `POST /v1/admin/jobs/tick` — the same job engine as
`rova jobs tick`, exposed over HTTP so tests (and this session's own
end-to-end acceptance test) can drive time without a cron. Non-production
only, per spec."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from rova.auth.principal import Principal, require_roles
from rova.config import get_settings
from rova.core.errors import ApiError
from rova.domain.enums import RoleCode
from rova.jobs.tick import JOBS, tick

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class TickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: str | None = None


@router.post("/jobs/tick")
def run_jobs_tick(body: TickBody = TickBody(),
                   principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN))):
    if get_settings().env == "production":
        raise ApiError("FORBIDDEN", "admin job tick is disabled in production")
    if body.job:
        if body.job not in JOBS:
            raise ApiError("VALIDATION_ERROR", f"unknown job {body.job!r}")
        from rova.jobs.tick import wire_hooks
        wire_hooks()
        fired = JOBS[body.job]()
        return {"job": body.job, "fired": fired}
    tick()
    return {"job": "all", "fired": None}
