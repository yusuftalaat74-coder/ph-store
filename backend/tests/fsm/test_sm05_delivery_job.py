"""A17 'Every state machine' row for SM-05 (§6 SM-05) — delivery_job.status:
ASSIGN, START, the three split ATTEMPT_* triggers (retry/attempt_count vs
CFG-DELIVERY-MAX-ATTEMPTS), RESCHEDULE, EXHAUST_WHILE_PENDING (no new attempt
recorded — attempt_count already at max), PARENT_CANCELLED, and the
RETURNED_TO_VENDOR -> Return(DELIVERY_EXHAUSTED) side effect."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-05"]

DISPATCHER = Principal(user_id=None, roles=frozenset({"Dispatcher"}))
COURIER = Principal(user_id=None, roles=frozenset({"Courier"}))


@pytest.fixture
def job(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm5_v', '951', 'V5', 'v5', 'VENDOR'), ('org_sm5_p', '952', 'P5', 'p5', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES ('ven_sm5', 'org_sm5_v', 'MAPUTO_CIDADE', 'V5', 'IMPORTER_WHOLESALER', "
        "'VENDOR_OWN_FLEET', 0)"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_sm5', 'org_sm5_p', 'MAPUTO_CIDADE', 'A', 'P5', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_sm5', 'D5', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x5', 'd5')"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_sm5', 'RQ-SM5-1', 'pha_sm5', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_sm5', 'req_sm5', 'idx_sm5', 3)"
    ))
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES ('ord_sm5', 'RQ-SM5-1-V01', 'req_sm5', 'ven_sm5', 'pha_sm5', "
        "'DISPATCHED', 'UPFRONT', 'VENDOR_OWN_FLEET', 60)"
    ))
    session.execute(text(
        "INSERT INTO delivery_job (id, order_id, vendor_id, pharmacy_id, performed_by, status) VALUES "
        "('dlv_sm5', 'ord_sm5', 'ven_sm5', 'pha_sm5', 'VENDOR_OWN_FLEET', 'CREATED')"
    ))
    yield "dlv_sm5"
    session.rollback()


def test_assign_then_start_legal(session, job):
    row = MACHINE.apply(session, job, "ASSIGN", DISPATCHER)
    assert row["status"] == "ASSIGNED"
    row = MACHINE.apply(session, job, "START", COURIER)
    assert row["status"] == "IN_TRANSIT"


def test_illegal_transition_raises(session, job):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, job, "START", COURIER)  # can't start before ASSIGN
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, job):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, job, "ASSIGN", COURIER)
    assert exc.value.code == "FORBIDDEN"


def test_attempt_delivered_fires_sm03(session, job):
    MACHINE.apply(session, job, "ASSIGN", DISPATCHER)
    MACHINE.apply(session, job, "START", COURIER)
    row = MACHINE.apply(session, job, "ATTEMPT_DELIVERED", COURIER)
    assert row["status"] == "DELIVERED"
    assert row["attempt_count"] == 1
    order_status = session.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": "ord_sm5"}).scalar()
    assert order_status == "DELIVERED_PENDING_RECEIPT"


def test_retry_then_final_exhausts_after_max_attempts(session, job):
    # CFG-DELIVERY-MAX-ATTEMPTS is seeded as 3 (A-07): two retries, then the
    # third failure must use ATTEMPT_FAILED_FINAL, not _RETRY (guard fails).
    MACHINE.apply(session, job, "ASSIGN", DISPATCHER)
    MACHINE.apply(session, job, "START", COURIER)
    row = MACHINE.apply(session, job, "ATTEMPT_FAILED_RETRY", COURIER)
    assert row["status"] == "FAILED_RETRY_PENDING" and row["attempt_count"] == 1

    row = MACHINE.apply(session, job, "RESCHEDULE", DISPATCHER)
    assert row["status"] == "ASSIGNED"
    MACHINE.apply(session, job, "START", COURIER)
    row = MACHINE.apply(session, job, "ATTEMPT_FAILED_RETRY", COURIER)
    assert row["status"] == "FAILED_RETRY_PENDING" and row["attempt_count"] == 2

    MACHINE.apply(session, job, "RESCHEDULE", DISPATCHER)
    MACHINE.apply(session, job, "START", COURIER)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, job, "ATTEMPT_FAILED_RETRY", COURIER)  # 3rd would hit max -> guard fails
    assert exc.value.code == "GUARD_FAILED"

    # the failed guard left the job IN_TRANSIT (attempt_count still 2) — the
    # next attempt on the same trip must use ATTEMPT_FAILED_FINAL instead.
    row = MACHINE.apply(session, job, "ATTEMPT_FAILED_FINAL", COURIER)
    assert row["status"] == "RETURNED_TO_VENDOR" and row["attempt_count"] == 3
    order_status = session.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": "ord_sm5"}).scalar()
    assert order_status == "CANCELLED"
    ret = session.execute(text('SELECT * FROM "return" WHERE order_id=:o'), {"o": "ord_sm5"}).mappings().one()
    assert ret["origin"] == "DELIVERY_EXHAUSTED" and ret["status"] == "REQUESTED"
    assert ret["rma_number"].startswith("RMA-")


def test_exhaust_while_pending_records_no_new_attempt(session, job):
    session.execute(text("UPDATE delivery_job SET status='FAILED_RETRY_PENDING', attempt_count=3 WHERE id=:id"),
                     {"id": job})
    row = MACHINE.apply(session, job, "EXHAUST_WHILE_PENDING", "SYSTEM")
    assert row["status"] == "RETURNED_TO_VENDOR"
    assert row["attempt_count"] == 3  # unchanged — no attempt was actually made


def test_exhaust_while_pending_guard_fails_below_max(session, job):
    session.execute(text("UPDATE delivery_job SET status='FAILED_RETRY_PENDING', attempt_count=1 WHERE id=:id"),
                     {"id": job})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, job, "EXHAUST_WHILE_PENDING", "SYSTEM")
    assert exc.value.code == "GUARD_FAILED"


def test_defect2_second_start_after_reschedule_does_not_raise_go_live_409(session, job):
    """Defect 2 repro + fix proof. Before the fix, eta_service.py registered
    GO_LIVE on SM-05's ASSIGNED -> IN_TRANSIT (rova/domain/hooks.py), which
    is only legal from eta_estimate.status = COMMITTED
    (sm20_eta_estimate.py). The first START correctly moved the estimate
    COMMITTED -> IN_TRANSIT_LIVE; a failed attempt + RESCHEDULE put the job
    back at ASSIGNED, and the *second* START tried to fire GO_LIVE again
    from an estimate already at IN_TRANSIT_LIVE, raising 409
    ILLEGAL_TRANSITION and stranding the job in ASSIGNED forever (its only
    other exit was PARENT_CANCELLED). The fix removes the GO_LIVE hook
    outright (A18: GO_LIVE never fires in v1, backend-spec.md A4.2 row
    SM-20) — this test wires the hooks for real and gives the order an open
    estimate, which every *other* SM-05 test in this file never does (their
    shared `job` fixture never calls `create_provisional`), which is
    exactly how this bug escaped the pre-existing suite entirely."""
    from rova.domain.hooks import wire
    from rova.domain.machines.sm20_eta_estimate import create_provisional
    wire()

    eta_id = create_provisional(session, "ord_sm5")
    MACHINES["SM-20"].apply(session, eta_id, "COMMIT", "SYSTEM")

    MACHINE.apply(session, job, "ASSIGN", DISPATCHER)
    MACHINE.apply(session, job, "START", COURIER)  # first START: fine either way
    MACHINE.apply(session, job, "ATTEMPT_FAILED_RETRY", COURIER)
    MACHINE.apply(session, job, "RESCHEDULE", DISPATCHER)

    row = MACHINE.apply(session, job, "START", COURIER)  # second START: used to 409
    assert row["status"] == "IN_TRANSIT"

    row = MACHINE.apply(session, job, "ATTEMPT_DELIVERED", COURIER)
    assert row["status"] == "DELIVERED"

    order_status = session.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": "ord_sm5"}).scalar()
    assert order_status == "DELIVERED_PENDING_RECEIPT"

    # COMMITTED -> REALISED/MISSED directly (the spec's own documented v1
    # deviation) — no GO_LIVE/IN_TRANSIT_LIVE step was needed at all.
    eta_status = session.execute(text("SELECT status FROM eta_estimate WHERE id=:id"), {"id": eta_id}).scalar()
    assert eta_status in ("REALISED", "MISSED")


def test_parent_cancelled_guard(session, job):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, job, "PARENT_CANCELLED", "SYSTEM")
    assert exc.value.code == "GUARD_FAILED"
    session.execute(text("UPDATE \"order\" SET status='CANCELLED', cancel_reason='PHARMACY' WHERE id='ord_sm5'"))
    row = MACHINE.apply(session, job, "PARENT_CANCELLED", "SYSTEM")
    assert row["status"] == "CANCELLED"
