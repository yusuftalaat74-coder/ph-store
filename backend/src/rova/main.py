"""create_app() -> FastAPI; mounts every router under /v1 (A2.14).

The product is named **PH Store**. The Python package, the database id
prefixes and the `ROVA_` environment prefix keep the original internal
codename `rova`: renaming them would touch every module and every migration
for no functional gain, and a forward-only migration cannot rename id
prefixes already written into rows. The brand lives in `APP_TITLE`; the
codename lives in the import path. Nothing user-facing says `ROVA`.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from rova.admin.router import router as admin_router
from rova.assistant.router import router as assistant_router
from rova.auth.router import router as auth_router
from rova.billing.router import router as billing_router
from rova.catalogue.router import router as catalogue_router
from rova.ordering.cart_router import router as cart_router
from rova.config import get_settings
from rova.core.errors import install_error_handlers
from rova.core.json_encoding import install_decimal_string_encoder
from rova.domain.hooks import wire as wire_hooks
from rova.fulfilment.router import router as fulfilment_router
from rova.health_router import router as health_router
from rova.intake.router import router as intake_router
from rova.integrations.office.router import router as office_router
from rova.notifications.router import router as notifications_router
from rova.onboarding.router import router as onboarding_router
from rova.ops.router import router as ops_router
from rova.ordering.router import router as ordering_router
from rova.pricelist.router import router as pricelist_router
from rova.resolution.router import router as resolution_router
from rova.support.router import router as support_router

APP_TITLE = "PH Store API"
APP_VERSION = "1.0.0"


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=APP_VERSION)

    # The Android client's page origin is the literal `null` (it is loaded
    # from file:///android_asset/), which only a wildcard matches.
    # `allow_credentials` is False and must stay False: this API carries its
    # token in an Authorization header, never a cookie, so there is nothing
    # for a browser to attach automatically to a cross-origin request.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origin_list(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Idempotency-Replayed"],
    )

    install_error_handlers(app)
    install_decimal_string_encoder()  # B fix 1b — Decimal (money and otherwise) is a JSON string, never a number
    wire_hooks()

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(ordering_router)
    app.include_router(cart_router)
    app.include_router(catalogue_router)
    app.include_router(fulfilment_router)
    app.include_router(billing_router)
    app.include_router(admin_router)
    app.include_router(pricelist_router)
    app.include_router(onboarding_router)
    app.include_router(intake_router)
    app.include_router(resolution_router)
    app.include_router(support_router)
    app.include_router(notifications_router)
    app.include_router(ops_router)
    app.include_router(assistant_router)
    app.include_router(office_router)

    # The pharmacy client, served from this same origin so a pharmacist can
    # open it in a browser instead of installing the APK. Mounted last so it
    # can never shadow an API route, and only when `ui_dir` is set and real —
    # a missing directory leaves the API exactly as it was rather than
    # refusing to boot. StaticFiles adds nothing to the OpenAPI schema, so
    # the endpoint inventory is unaffected.
    ui_dir = get_settings().ui_dir.strip()
    if ui_dir and Path(ui_dir).is_dir():
        app.mount("/app", StaticFiles(directory=ui_dir, html=True), name="app")

    return app
