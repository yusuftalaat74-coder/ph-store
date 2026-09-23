"""A14.6 job-engine tests (Scope B) — a timer past `expires_at` produces the
right transition; a CANCELLED (or not-yet-due) timer produces none. Fixture
rows are inserted through `db_engine.begin()` (auto-committing, its own
connection) rather than the rollback-at-teardown `session` fixture, because
each job under test opens its own SQLAlchemy session via
`rova.core.db.get_sessionmaker()` — a second, separate connection to the
same `rova_test_<pid>` database — and would not see uncommitted rows on the
test's own connection (the same convention `tests/api/test_checkout_idempotency.py`
already uses for its own cross-connection setup)."""
from datetime import timedelta

from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.jobs import tick as jobs


def _org_vendor_pharmacy(db_engine, suffix):
    org_v, org_p = new_id("org"), new_id("org")
    ven_id, pha_id = new_id("ven"), new_id("pha")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"(:v, :v, 'V{suffix}', 'v{suffix}', 'VENDOR'), (:p, :p, 'P{suffix}', 'p{suffix}', 'PHARMACY')"
        ), {"v": org_v, "p": org_p})
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, status) VALUES "
            f"(:id, :o, 'MAPUTO_CIDADE', 'V{suffix}', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'ONBOARDING')"
        ), {"id": ven_id, "o": org_v})
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            f"address, latitude, longitude) VALUES (:id, :o, 'MAPUTO_CIDADE', 'A', 'P{suffix}', 'X', 0, 0)"
        ), {"id": pha_id, "o": org_p})
    return ven_id, pha_id


def test_quotation_sla_fires_quote_timeout_on_expired_timer(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt1")
    quo_id = new_id("quo")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            "(:id, 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:jt1', 'd')"
        ), {"id": "idx_jt1"})
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            "(:id, 'RQ-JT1', :p, 'RFQ', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"id": "req_jt1", "p": pha_id})
        conn.execute(text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
            "(:id, 'req_jt1', 'idx_jt1', 5)"
        ), {"id": "rql_jt1"})
        conn.execute(text(
            "INSERT INTO quotation (id, request_id, vendor_id, status, expires_at) VALUES "
            "(:id, 'req_jt1', :v, 'INVITED', :exp)"
        ), {"id": quo_id, "v": ven_id, "exp": now() - timedelta(hours=1)})
        conn.execute(text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source, status) "
            "VALUES (:id, 'QUOTATION', 'quotation', :q, :exp, 'CFG-SLA-QUOTATION-HOURS', 'RUNNING')"
        ), {"id": new_id("sla"), "q": quo_id, "exp": now() - timedelta(hours=1)})

    fired = jobs.quotation_sla()
    assert fired == 1

    row = session.execute(text("SELECT status FROM quotation WHERE id=:id"), {"id": quo_id}).mappings().one()
    assert row["status"] == "EXPIRED"
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE subject_id=:id"), {"id": quo_id}
    ).mappings().one()
    assert timer["status"] == "EXPIRED"


def test_quotation_sla_skips_cancelled_timer(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt2")
    quo_id = new_id("quo")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            "(:id, 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:jt2', 'd')"
        ), {"id": "idx_jt2"})
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            "(:id, 'RQ-JT2', :p, 'RFQ', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"id": "req_jt2", "p": pha_id})
        conn.execute(text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
            "(:id, 'req_jt2', 'idx_jt2', 5)"
        ), {"id": "rql_jt2"})
        # already SUBMITTED, as if SUBMIT's own effect had cancelled the timer
        conn.execute(text(
            "INSERT INTO quotation (id, request_id, vendor_id, status, expires_at) VALUES "
            "(:id, 'req_jt2', :v, 'SUBMITTED', :exp)"
        ), {"id": quo_id, "v": ven_id, "exp": now() - timedelta(hours=1)})
        conn.execute(text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source, status) "
            "VALUES (:id, 'QUOTATION', 'quotation', :q, :exp, 'CFG-SLA-QUOTATION-HOURS', 'CANCELLED')"
        ), {"id": new_id("sla"), "q": quo_id, "exp": now() - timedelta(hours=1)})

    fired = jobs.quotation_sla()
    assert fired == 0

    row = session.execute(text("SELECT status FROM quotation WHERE id=:id"), {"id": quo_id}).mappings().one()
    assert row["status"] == "SUBMITTED"  # untouched — CANCELLED timers are never picked up


def test_reroute_ask_timeout_drops_line_on_expired_timer(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt3")
    line_id = new_id("orl")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            "(:id, 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:jt3', 'd')"
        ), {"id": "idx_jt3"})
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            "(:id, 'RQ-JT3', :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"id": "req_jt3", "p": pha_id})
        conn.execute(text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
            "(:id, 'req_jt3', 'idx_jt3', 6)"
        ), {"id": "rql_jt3"})
        conn.execute(text(
            "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
            "delivery_mode, goods_total) VALUES (:id, 'RQ-JT3-V01', 'req_jt3', :v, :p, 'PENDING_ACCEPTANCE', "
            "'UPFRONT', 'VENDOR_OWN_FLEET', 120)"
        ), {"id": "ord_jt3", "v": ven_id, "p": pha_id})
        conn.execute(text(
            "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
            "ordered_qty, confirmed_qty, unit_price, price_source, price_source_id, fulfilment_status) VALUES "
            "(:id, 'ord_jt3', 'rql_jt3', 'idx_jt3', false, 6, 2, 20.00, 'VENDOR_OFFER', 'ofr_jt3', 'LINE_SHORT')"
        ), {"id": line_id})
        conn.execute(text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source, status) "
            "VALUES (:id, 'REROUTE_ASK', 'order_line', :l, :exp, 'CFG-REROUTE-ASK-TIMEOUT-HOURS', 'RUNNING')"
        ), {"id": new_id("sla"), "l": line_id, "exp": now() - timedelta(hours=1)})

    fired = jobs.reroute_ask_timeout()
    assert fired == 1

    row = session.execute(
        text("SELECT fulfilment_status FROM order_line WHERE id=:id"), {"id": line_id}
    ).mappings().one()
    assert row["fulfilment_status"] == "LINE_DROPPED"


def test_reroute_ask_timeout_ignores_timer_not_yet_due(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt4")
    line_id = new_id("orl")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            "(:id, 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:jt4', 'd')"
        ), {"id": "idx_jt4"})
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            "(:id, 'RQ-JT4', :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"id": "req_jt4", "p": pha_id})
        conn.execute(text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
            "(:id, 'req_jt4', 'idx_jt4', 6)"
        ), {"id": "rql_jt4"})
        conn.execute(text(
            "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
            "delivery_mode, goods_total) VALUES (:id, 'RQ-JT4-V01', 'req_jt4', :v, :p, 'PENDING_ACCEPTANCE', "
            "'UPFRONT', 'VENDOR_OWN_FLEET', 120)"
        ), {"id": "ord_jt4", "v": ven_id, "p": pha_id})
        conn.execute(text(
            "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
            "ordered_qty, confirmed_qty, unit_price, price_source, price_source_id, fulfilment_status) VALUES "
            "(:id, 'ord_jt4', 'rql_jt4', 'idx_jt4', false, 6, 2, 20.00, 'VENDOR_OFFER', 'ofr_jt4', 'LINE_SHORT')"
        ), {"id": line_id})
        conn.execute(text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source, status) "
            "VALUES (:id, 'REROUTE_ASK', 'order_line', :l, :exp, 'CFG-REROUTE-ASK-TIMEOUT-HOURS', 'RUNNING')"
        ), {"id": new_id("sla"), "l": line_id, "exp": now() + timedelta(hours=4)})  # not due yet

    fired = jobs.reroute_ask_timeout()
    assert fired == 0
    row = session.execute(
        text("SELECT fulfilment_status FROM order_line WHERE id=:id"), {"id": line_id}
    ).mappings().one()
    assert row["fulfilment_status"] == "LINE_SHORT"  # unchanged


def _order_with_receipt_line(db_engine, suffix, pha_id, ven_id):
    order_id, line_id, receipt_id, rline_id = (new_id("ord"), new_id("orl"), new_id("rcp"), new_id("rcl"))
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            f"(:id, 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:{suffix}', 'd')"
        ), {"id": f"idx_{suffix}"})
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            f"(:id, 'RQ-{suffix}', :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"id": f"req_{suffix}", "p": pha_id})
        conn.execute(text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
            f"(:id, 'req_{suffix}', :i, 3)"
        ), {"id": f"rql_{suffix}", "i": f"idx_{suffix}"})
        conn.execute(text(
            "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
            f"delivery_mode, goods_total) VALUES (:id, 'RQ-{suffix}-V01', :r, :v, :p, 'RECEIPT_ACCEPTED', "
            "'UPFRONT', 'VENDOR_OWN_FLEET', 60)"
        ), {"id": order_id, "r": f"req_{suffix}", "v": ven_id, "p": pha_id})
        conn.execute(text(
            "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
            "ordered_qty, confirmed_qty, unit_price, price_source, price_source_id) VALUES "
            f"(:id, :o, :rl, :i, false, 3, 3, 20.00, 'VENDOR_OFFER', 'ofr_{suffix}')"
        ), {"id": line_id, "o": order_id, "rl": f"rql_{suffix}", "i": f"idx_{suffix}"})
        conn.execute(text(
            "INSERT INTO receipt (id, order_id) VALUES (:id, :o)"
        ), {"id": receipt_id, "o": order_id})
        conn.execute(text(
            "INSERT INTO receipt_line (id, receipt_id, order_line_id, accepted_qty) VALUES (:id, :r, :l, 3)"
        ), {"id": rline_id, "r": receipt_id, "l": line_id})
        conn.execute(text(
            "INSERT INTO state_transition (id, machine, subject_type, subject_id, from_state, to_state, "
            "trigger, actor_role, occurred_at) VALUES (:id, 'SM-03', 'order', :o, 'DELIVERED_PENDING_RECEIPT', "
            "'RECEIPT_ACCEPTED', 'RECEIPT_ACCEPT', 'PharmacyAdmin', :occ)"
        ), {"id": new_id("stt"), "o": order_id, "occ": now() - timedelta(hours=200)})
    return order_id, line_id, rline_id


def test_dispute_assignment_force_assigns_and_starts_resolution_timer(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt5")
    order_id, line_id, rline_id = _order_with_receipt_line(db_engine, "jt5", pha_id, ven_id)
    dsp_id = new_id("dsp")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO dispute (id, order_id, receipt_line_id, type, raised_by, status, created_at) VALUES "
            "(:id, :o, :rl, 'RECEIPT_DISCREPANCY', 'PharmacyReceiver', 'OPEN', :created)"
        ), {"id": dsp_id, "o": order_id, "rl": rline_id, "created": now() - timedelta(hours=48)})

    fired = jobs.dispute_assignment()
    assert fired == 1

    row = session.execute(
        text("SELECT status, assigned_at FROM dispute WHERE id=:id"), {"id": dsp_id}
    ).mappings().one()
    assert row["status"] == "UNDER_REVIEW" and row["assigned_at"] is not None
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='DISPUTE_RESOLUTION' AND subject_id=:id"), {"id": dsp_id}
    ).mappings().one()
    assert timer["status"] == "RUNNING"  # SM-09's ASSIGN/FORCE_ASSIGN effect (added this session) started it


def test_dispute_assignment_ignores_recent_open_dispute(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt6")
    order_id, line_id, rline_id = _order_with_receipt_line(db_engine, "jt6", pha_id, ven_id)
    dsp_id = new_id("dsp")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO dispute (id, order_id, receipt_line_id, type, raised_by, status, created_at) VALUES "
            "(:id, :o, :rl, 'RECEIPT_DISCREPANCY', 'PharmacyReceiver', 'OPEN', :created)"
        ), {"id": dsp_id, "o": order_id, "rl": rline_id, "created": now()})  # just raised

    fired = jobs.dispute_assignment()
    assert fired == 0
    row = session.execute(text("SELECT status FROM dispute WHERE id=:id"), {"id": dsp_id}).mappings().one()
    assert row["status"] == "OPEN"


def test_dispute_resolution_sla_escalates_on_expired_timer(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt7")
    order_id, line_id, rline_id = _order_with_receipt_line(db_engine, "jt7", pha_id, ven_id)
    dsp_id = new_id("dsp")
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO dispute (id, order_id, receipt_line_id, type, raised_by, status, assigned_at) VALUES "
            "(:id, :o, :rl, 'RECEIPT_DISCREPANCY', 'PharmacyReceiver', 'UNDER_REVIEW', :now)"
        ), {"id": dsp_id, "o": order_id, "rl": rline_id, "now": now()})
        conn.execute(text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source, status) "
            "VALUES (:id, 'DISPUTE_RESOLUTION', 'dispute', :d, :exp, 'CFG-DISPUTE-RESOLUTION-DAYS', 'RUNNING')"
        ), {"id": new_id("sla"), "d": dsp_id, "exp": now() - timedelta(hours=1)})

    fired = jobs.dispute_resolution_sla()
    assert fired == 1
    row = session.execute(text("SELECT status FROM dispute WHERE id=:id"), {"id": dsp_id}).mappings().one()
    assert row["status"] == "ESCALATED"


def test_verification_sla_queues_notification_once(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt8")
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE vendor_account SET created_at=:c WHERE id=:id"),
                     {"c": now() - timedelta(hours=200), "id": ven_id})

    queued_first = jobs.verification_sla()
    assert queued_first >= 1
    rows = session.execute(
        text("SELECT payload FROM notification WHERE event_code='N-VERIFICATION_SLA_BREACHED' "
             "AND payload->>'holder_id' = :h"),
        {"h": ven_id},
    ).mappings().all()
    assert len(rows) == 1  # exactly one queued for this holder

    queued_second = jobs.verification_sla()
    rows_after = session.execute(
        text("SELECT 1 FROM notification WHERE event_code='N-VERIFICATION_SLA_BREACHED' "
             "AND payload->>'holder_id' = :h"),
        {"h": ven_id},
    ).all()
    assert len(rows_after) == 1  # re-run does not duplicate


def test_invoice_upload_sla_queues_notification_for_stale_receipt(db_engine, session):
    ven_id, pha_id = _org_vendor_pharmacy(db_engine, "jt9")
    order_id, line_id, rline_id = _order_with_receipt_line(db_engine, "jt9", pha_id, ven_id)

    fired = jobs.invoice_upload_sla()
    # >= 1, not == 1: earlier tests in this module (jt5/jt6/jt7) also leave
    # behind RECEIPT_ACCEPTED orders with no invoice (this file's fixtures
    # are committed, not rolled back, per the module docstring) — what
    # matters here is that *this* order was among the ones notified.
    assert fired >= 1
    row = session.execute(
        text("SELECT recipient_role FROM notification WHERE event_code='N-INVOICE_UPLOAD_SLA_BREACHED' "
             "AND payload->>'order_id' = :o"),
        {"o": order_id},
    ).mappings().one()
    assert row["recipient_role"] == "VendorFinance"


def test_housekeeping_runs_without_error():
    jobs.housekeeping()  # smoke test: idempotency_key/auth_session cleanup runs clean on an empty table
