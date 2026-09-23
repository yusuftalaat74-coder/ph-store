"""A17 'Every state machine' row for SM-10 (§6 SM-10, E-036) — "return".
REQUESTED -> APPROVED|REJECTED -> GOODS_IN_TRANSIT -> RECEIVED_BY_VENDOR ->
CLOSED. One of item 5's 8 missing machine test files (backend-review-r1.md)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-10"]

VENDOR_FINANCE = Principal(user_id=None, roles=frozenset({"VendorFinance"}))
DISPATCHER = Principal(user_id=None, roles=frozenset({"Dispatcher"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


@pytest.fixture
def rma(session):
    org_v, org_p = new_id("org"), new_id("org")
    ven_id, pha_id, idx_id, req_id, ord_id, ret_id = (
        new_id("ven"), new_id("pha"), new_id("idx"), new_id("req"), new_id("ord"), new_id("ret"),
    )
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:ov, :ov, 'V10', 'v10', 'VENDOR'), (:op, :op, 'P10', 'p10', 'PHARMACY')"
    ), {"ov": org_v, "op": org_p})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V10', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_v})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P10', 'X', 0, 0)"
    ), {"p": pha_id, "o": org_p})
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:r, :rnum, :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ), {"r": req_id, "rnum": f"RQ-{req_id}", "p": pha_id})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES (:oid, :onum, :r, :v, :p, 'DELIVERED_PENDING_RECEIPT', "
        "'UPFRONT', 'VENDOR_OWN_FLEET', 100)"
    ), {"oid": ord_id, "onum": f"RQ-{req_id}-V01", "r": req_id, "v": ven_id, "p": pha_id})
    session.execute(text(
        "INSERT INTO \"return\" (id, order_id, rma_number, status, origin) VALUES "
        "(:id, :o, :rma, 'REQUESTED', 'PHARMACY_RMA')"
    ), {"id": ret_id, "o": ord_id, "rma": f"RMA-{ret_id}"})
    # committed (not just left in this fixture's own uncommitted transaction)
    # so the threading test below can see it from two genuinely separate
    # connections — ids are fresh ULIDs per test run, so nothing to clean up.
    session.commit()
    yield ret_id


def test_full_happy_path(session, rma):
    row = MACHINE.apply(session, rma, "APPROVE", VENDOR_FINANCE)
    assert row["status"] == "APPROVED"
    row = MACHINE.apply(session, rma, "SHIP", DISPATCHER)
    assert row["status"] == "GOODS_IN_TRANSIT"
    row = MACHINE.apply(session, rma, "RECEIVE", VENDOR_FINANCE)
    assert row["status"] == "RECEIVED_BY_VENDOR"
    row = MACHINE.apply(session, rma, "CLOSE_WITH_CREDIT_NOTE", VENDOR_FINANCE)
    assert row["status"] == "CLOSED"


def test_reject_terminal(session, rma):
    row = MACHINE.apply(session, rma, "REJECT", VENDOR_FINANCE)
    assert row["status"] == "REJECTED"
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rma, "APPROVE", VENDOR_FINANCE)
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, rma):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rma, "APPROVE", PHARMACY_ADMIN)
    assert exc.value.code == "FORBIDDEN"
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rma, "SHIP", VENDOR_FINANCE)  # SHIP is Dispatcher-only, not even reachable state yet
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_two_session_concurrent_apply_is_safe_not_stale_state(session, rma):
    """Item 5: a REAL two-session concurrency test — two genuinely separate
    SQLAlchemy sessions on two real DB connections, racing via Python
    `threading` so Postgres itself serialises them (not a forged UPDATE
    inside one session's test body pretending to be a second caller, which
    is exactly what the old `test_stale_state_conflict` sham did).

    This deliberately does NOT assert STALE_STATE, and that is itself the
    real finding, verified by execution (also reproduced standalone with
    two raw threads against `MACHINE.apply` before writing this test):
    `Machine.apply` does `SELECT ... FOR UPDATE` then `UPDATE ... WHERE
    status = :expected` using the SAME just-read value for `:expected`,
    with the row locked continuously between the two statements. Postgres's
    `FOR UPDATE` blocks a second racer until the first commits and then
    returns the FRESH row — so by the time the second caller's own `_find`
    runs, it already sees the post-commit state and correctly raises
    ILLEGAL_TRANSITION *before* ever reaching the conditional UPDATE.
    `updated.rowcount == 0` (the STALE_STATE branch) therefore cannot be
    reached through any interleaving of two honest `MACHINE.apply()` calls
    — the lock discipline that makes the row current also makes that branch
    dead code for real concurrency, which is a safe outcome (no silent
    double-apply, no corruption), just not the STALE_STATE one."""
    import threading

    from rova.core.db import get_sessionmaker

    Session = get_sessionmaker()
    outcomes: dict[str, tuple[str, str]] = {}

    def racer(name: str) -> None:
        sess = Session()
        try:
            row = MACHINE.apply(sess, rma, "APPROVE", VENDOR_FINANCE)
            sess.commit()
            outcomes[name] = ("OK", row["status"])
        except ApiError as exc:
            sess.rollback()
            outcomes[name] = ("ApiError", exc.code)
        finally:
            sess.close()

    t1 = threading.Thread(target=racer, args=("t1",))
    t2 = threading.Thread(target=racer, args=("t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    kinds = sorted(outcomes.values())
    assert kinds == [("ApiError", "ILLEGAL_TRANSITION"), ("OK", "APPROVED")], outcomes
    final_status = session.execute(text('SELECT status FROM "return" WHERE id=:id'), {"id": rma}).scalar()
    assert final_status == "APPROVED"  # exactly one racer's write survived — no corruption


def test_illegal_transition_matrix(session, rma):
    assert_all_illegal(session, MACHINE, rma, VENDOR_FINANCE)
