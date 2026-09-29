"""A2.11 — one error envelope for every non-2xx response."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

CODES = {
    "VALIDATION_ERROR": 422,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "ILLEGAL_TRANSITION": 409,
    "GUARD_FAILED": 409,
    "STALE_STATE": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "CONFLICT": 409,
    # The cart is a DRAFT that can sit for days, so what it would bill is
    # checked against what the pharmacist was shown. Its own code because the
    # client reacts differently from any other guard: it reloads, shows the
    # difference, and asks.
    "PRICE_MOVED": 409,
    # A pharmacy that signs itself up gets its own error for a number that
    # already has an account, because the client answers it with "sign in
    # instead" rather than a generic conflict (owner decision: this is the
    # one fact sign-up is allowed to reveal).
    "PHONE_ALREADY_REGISTERED": 409,
    # Sign-up is the only unauthenticated write; it is rate limited per IP
    # and per phone from the database (two uvicorn workers).
    "RATE_LIMITED": 429,
    # Checkout by a pharmacy whose account a reviewer has not approved yet
    # (or that was suspended/closed). Browsing and the cart keep working.
    "PHARMACY_NOT_ACTIVE": 403,
    "NOT_IMPLEMENTED": 501,
    "INTERNAL": 500,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, *, details: list[dict] | None = None, rule: str | None = None):
        if code not in CODES:
            raise ValueError(f"unknown error code {code}")
        self.code = code
        self.status_code = CODES[code]
        self.message = message
        self.details = details or []
        self.rule = rule
        super().__init__(message)

    def body(self) -> dict:
        d = {"error": {"code": self.code, "message": self.message, "details": self.details}}
        if self.rule:
            d["error"]["rule"] = self.rule
        return d


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "reason": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "message": "invalid request", "details": details}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL", "message": "internal error", "details": []}})
