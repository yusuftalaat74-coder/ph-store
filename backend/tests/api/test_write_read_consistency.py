"""Defect 1 proof — the same way the architect found it: a real HTTP server
(not `TestClient`, which never actually races anything — its ASGI transport
call and the fixture's own docstring both note it only rolls tests back, it
never sends bytes over a socket while a `Depends`-with-yield exit stack is
still open) driving 30 write-then-immediately-read cycles from a separate
connection.

Before the fix: `rova/core/db.py::get_session` was injected everywhere as
bare `Depends(get_session)`, whose default scope in this FastAPI version is
"request" — the exit stack that runs its post-`yield` code (the commit)
closes only *after* `await response(scope, receive, send)` has already sent
the response (`fastapi/routing.py::request_response`: `function_astack`
closes before the response is awaited, `request_astack` after). A client
reading immediately after its own 200 could see the write not yet
committed, and a commit failure would surface after a 200 was already on
the wire. The fix is `Depends(get_session, scope="function")` everywhere
(`rova/core/db.py`'s own docstring), which moves the commit onto
`function_astack` — closed before the response is sent."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from rova.auth.security import hash_password

SUFFIX = "wrc1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_pharmacy_user(db_engine, suffix: str) -> str:
    phone = f"+258840099{suffix}"
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_wrc_{suffix}', '77{suffix}9', 'WRC Pharmacy', 'wrc pharmacy', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
            "latitude, longitude) VALUES "
            f"('pha_wrc_{suffix}', 'org_wrc_{suffix}', 'MAPUTO_CIDADE', 'A', 'WRC Pharmacy', 'X', 0, 0)"
        ))
        conn.execute(
            text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :phone, :name, :h)"),
            {"id": f"usr_wrc_{suffix}", "phone": phone, "name": "WRC Buyer", "h": hash_password("rova-demo")},
        )
        conn.execute(
            text("INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES (:mid, :uid, :org, :roles)"),
            {"mid": f"mem_wrc_{suffix}", "uid": f"usr_wrc_{suffix}", "org": f"org_wrc_{suffix}",
             "roles": ["PharmacyBuyer"]},
        )
    return phone


@pytest.fixture
def live_server(app):
    """A real uvicorn server bound to a real loopback socket, in a
    background thread of this same test process (so it shares the module-
    level engine/sessionmaker singletons in rova.core.db, same as any other
    test here) — the only way to reproduce the actual race defect 1 was
    about, since TestClient never puts bytes on a wire at all."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start in time"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_30_write_then_immediate_read_cycles_have_zero_stale_reads(live_server, db_engine):
    phone = _setup_pharmacy_user(db_engine, SUFFIX)
    N = 30

    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        stale_ids = []
        for i in range(N):
            r = client.post(
                "/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"}, headers=headers
            )
            assert r.status_code == 200, r.text
            request_id = r.json()["id"]
            # Immediately, from a wholly separate DB connection (db_engine,
            # never touched by the app's own sessionmaker) — exactly the
            # architect's probe: "30 x POST /v1/requests each followed by
            # an immediate SELECT".
            with db_engine.connect() as conn:
                found = conn.execute(text("SELECT 1 FROM request WHERE id=:id"), {"id": request_id}).first()
            if found is None:
                stale_ids.append((i, request_id))

    assert stale_ids == [], (
        f"{len(stale_ids)}/{N} writes were not visible in a separate connection immediately "
        f"after their own 200 OK (defect 1: commit ran after the response was sent): {stale_ids}"
    )
