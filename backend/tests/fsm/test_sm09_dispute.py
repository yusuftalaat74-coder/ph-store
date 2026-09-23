"""A17 'Every state machine' row for SM-09 (§6 SM-09) — dispute. OPEN ->
UNDER_REVIEW (ASSIGN/FORCE_ASSIGN) -> {ESCALATED, RESOLVED}; ESCALATED ->
RESOLVED. Covers the SlaTimer(dispute_resolution) start/cancel side effects
and the R-074 ComplianceOfficer-only guard for REGULATED_PRICE_INCIDENT.
One of item 5's 8 missing machine test files (backend-review-r1.md)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-09"]
POLICY = "DISPUTE_RESOLUTION"

VENDOR_FINANCE = Principal(user_id=None, roles=frozenset({"VendorFinance"}))
COMPLIANCE = Principal(user_id=None, roles=frozenset({"ComplianceOfficer"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


def _make_dispute(session, *, dispute_type: str) -> str:
    org_v, org_p = new_id("org"), new_id("org")
    ven_id, pha_id, req_id, ord_id, dsp_id = (
        new_id("ven"), new_id("pha"), new_id("req"), new_id("ord"), new_id("dsp"),
    )
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:ov, :ov, 'V9', 'v9', 'VENDOR'), (:op, :op, 'P9', 'p9', 'PHARMACY')"
    ), {"ov": org_v, "op": org_p})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V9', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_v})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P9', 'X', 0, 0)"
    ), {"p": pha_id, "o": org_p})
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:r, :rnum, :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ), {"r": req_id, "rnum": f"RQ-{req_id}", "p": pha_id})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES (:oid, :onum, :r, :v, :p, 'DELIVERED_PENDING_RECEIPT', 'UPFRONT', "
        "'VENDOR_OWN_FLEET', 100)"
    ), {"oid": ord_id, "onum": f"RQ-{req_id}-V01", "r": req_id, "v": ven_id, "p": pha_id})
    # ck_dispute_subject: exactly one of receipt_line_id/invoice_line_id must
    # be set — build a minimal receipt_line to hang the dispute off (R-076's
    # RECEIPT_DISPUTE path, the common case for all three DisputeType values
    # in this reduced fixture).
    idx_id, rql_id, orl_id, rcp_id, rcl_id = (
        new_id("idx"), new_id("rql"), new_id("orl"), new_id("rcp"), new_id("rcl"),
    )
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "(:p, 'D9', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x9', 'd9')"
    ), {"p": idx_id})
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES (:id, :r, :p, 5)"
    ), {"id": rql_id, "r": req_id, "p": idx_id})
    session.execute(text(
        "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, ordered_qty, "
        "unit_price, price_source, price_source_id) VALUES "
        "(:id, :o, :rl, :p, false, 5, 10.00, 'VENDOR_OFFER', 'ofr_x')"
    ), {"id": orl_id, "o": ord_id, "rl": rql_id, "p": idx_id})
    session.execute(text(
        "INSERT INTO receipt (id, order_id, received_at) VALUES (:id, :o, now())"
    ), {"id": rcp_id, "o": ord_id})
    session.execute(text(
        "INSERT INTO receipt_line (id, receipt_id, order_line_id, accepted_qty, rejected_qty, reason_code) "
        "VALUES (:id, :r, :ol, 3, 2, 'DAMAGED')"
    ), {"id": rcl_id, "r": rcp_id, "ol": orl_id})
    session.execute(text(
        "INSERT INTO dispute (id, order_id, receipt_line_id, type, raised_by, status) VALUES "
        "(:id, :o, :rcl, :t, 'PharmacyReceiver', 'OPEN')"
    ), {"id": dsp_id, "o": ord_id, "rcl": rcl_id, "t": dispute_type})
    session.commit()
    return dsp_id


@pytest.fixture
def dispute(session):
    return _make_dispute(session, dispute_type="RECEIPT_DISCREPANCY")


@pytest.fixture
def regulated_price_dispute(session):
    return _make_dispute(session, dispute_type="REGULATED_PRICE_INCIDENT")


def test_assign_starts_resolution_timer_then_resolve_cancels_it(session, dispute):
    row = MACHINE.apply(session, dispute, "ASSIGN", VENDOR_FINANCE)
    assert row["status"] == "UNDER_REVIEW" and row["assigned_to_user_id"] is None
    assert timers.is_running(session, policy_type=POLICY, subject_type="dispute", subject_id=dispute)

    row = MACHINE.apply(session, dispute, "RESOLVE", VENDOR_FINANCE, outcome="CREDIT_NOTE", notes="ok")
    assert row["status"] == "RESOLVED" and row["outcome"] == "CREDIT_NOTE"
    assert not timers.is_running(session, policy_type=POLICY, subject_type="dispute", subject_id=dispute)


def test_force_assign_by_system(session, dispute):
    row = MACHINE.apply(session, dispute, "FORCE_ASSIGN", "SYSTEM")
    assert row["status"] == "UNDER_REVIEW"
    assert timers.is_running(session, policy_type=POLICY, subject_type="dispute", subject_id=dispute)


def test_escalate_cancels_timer(session, dispute):
    MACHINE.apply(session, dispute, "ASSIGN", VENDOR_FINANCE)
    row = MACHINE.apply(session, dispute, "ESCALATE", VENDOR_FINANCE)
    assert row["status"] == "ESCALATED"
    assert not timers.is_running(session, policy_type=POLICY, subject_type="dispute", subject_id=dispute)
    row = MACHINE.apply(session, dispute, "RESOLVE_ESCALATED", COMPLIANCE, outcome="REJECTED")
    assert row["status"] == "RESOLVED" and row["outcome"] == "REJECTED"


def test_resolve_guard_requires_valid_outcome(session, dispute):
    MACHINE.apply(session, dispute, "ASSIGN", VENDOR_FINANCE)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, dispute, "RESOLVE", VENDOR_FINANCE, outcome="NOT_A_REAL_OUTCOME")
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-076"


def test_resolve_guard_regulated_price_incident_requires_compliance(session, regulated_price_dispute):
    MACHINE.apply(session, regulated_price_dispute, "ASSIGN", COMPLIANCE)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, regulated_price_dispute, "RESOLVE", VENDOR_FINANCE, outcome="CREDIT_NOTE")
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-074"
    row = MACHINE.apply(session, regulated_price_dispute, "RESOLVE", COMPLIANCE, outcome="CREDIT_NOTE")
    assert row["status"] == "RESOLVED"


def test_wrong_actor_forbidden(session, dispute):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, dispute, "ASSIGN", PHARMACY_ADMIN)
    assert exc.value.code == "FORBIDDEN"


def test_illegal_transition_matrix(session, dispute):
    from datetime import datetime, timezone

    assert_all_illegal(
        session, MACHINE, dispute, VENDOR_FINANCE,
        extra_by_state={"RESOLVED": {"outcome": "REJECTED", "resolved_at": datetime.now(timezone.utc)}},
    )
