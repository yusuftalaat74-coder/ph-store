"""A14.6 `rova jobs tick`.

Implemented subset (this session, job-engine / Scope B — see the executor
manifest): `housekeeping` (job 12, pre-existing) plus the SLA-timer-driven
jobs named in the brief:

  quotation_sla            SM-02 QUOTE_TIMEOUT   on expired QUOTATION timers
  acceptance_sla            SM-03 ACCEPTANCE_TIMEOUT on expired ACCEPTANCE timers
  dispatch_sla               SM-03 DISPATCH_TIMEOUT  on expired DISPATCH timers
  reroute_ask_timeout         SM-04 DROP               on expired REROUTE_ASK timers
  dispute_assignment           SM-09 FORCE_ASSIGN       R-124: OPEN disputes past
                                CFG-DISPUTE-ASSIGNMENT-HOURS since creation — scanned
                                off `dispute.created_at` directly rather than an
                                sla_timer row, because R-124's whole point is that
                                the SlaTimer(dispute_resolution) row "does not start
                                until actual assignment" (§6 SM-09 prose) — there is
                                no DISPUTE_ASSIGNMENT entry in sla_timer's own
                                policy_type CHECK, only DISPUTE_RESOLUTION.
  dispute_resolution_sla       SM-09 ESCALATE           on expired DISPUTE_RESOLUTION
                                timers. Not one of the 8 items named in the task's own
                                list, but included because the SlaTimer(dispute_resolution)
                                start/cancel effects the spec's SM-09 transition table
                                requires (§6 SM-09) were missing from sm09_dispute.py and
                                were added in this session — a timer that nothing ever
                                consumes is a job-engine gap of its own, so this closes it.
  verification_sla             notification-only (N-.., queued to the `notification`
                                outbox, A18-reduced — no delivery channel is wired):
                                vendor_account/pharmacy_account rows stuck ONBOARDING
                                past CFG-VERIFICATION-SLA-HOURS. SM-06/SM-07 have no
                                ONBOARDING-timeout *state* (both machines are outside
                                Scope A's 12 and their own docstrings already document
                                the reduction), so this cannot fire a transition without
                                inventing one on a machine out of scope — it queues the
                                escalation notice instead, exactly as invoice-upload SLA
                                does below.
  invoice_upload_sla           notification-only: orders sitting RECEIPT_ACCEPTED past
                                CFG-INVOICE-UPLOAD-SLA-HOURS with no invoice uploaded yet
                                (§16 CFG-INVOICE-UPLOAD-SLA-HOURS: "مهلة رفع المورّد
                                للفاتورة ... قبل التصعيد" — an escalation, not a state
                                change; SM-11's own states all start at UPLOADED).

  admin_approval_timeout        SM-01 ADMIN_REJECT on expired ADMIN_APPROVAL timers.
                                Wired now that something drives the branch: checkout
                                sends an over-threshold basket to AWAITING_ADMIN_APPROVAL
                                instead of refusing it. Without the job the timer
                                expires, `_guard_admin_approve` starts refusing because
                                it requires a running timer, and the basket sits in a
                                state nobody can approve and no screen can edit — the
                                same dead end the gate was wired to remove, one state
                                further along. The basket goes back to DRAFT with its
                                lines, because silence from an admin is not the pharmacy
                                deciding it does not want the medicines.

Every job here is idempotent and safe to run concurrently with itself: each
sla_timer row is claimed with `UPDATE ... WHERE status='RUNNING'` before the
machine transition is attempted (mirrors `uq_sla_timer_running`'s own
at-most-one-active-timer guarantee), and the notification-only scans key off
`NOT EXISTS` / precise cutoffs so a re-run before the next real event finds
nothing new to queue.

"Safe concurrently" means the database enforces it, not that the read and the
write happen to be close together: `cart_idle`'s once-per-basket rule is
`uq_one_idle_notice_per_cart` (migration 0007), so two overlapping ticks
produce one notice however the check-then-insert interleaves. The older scans
above still rely on their `NOT EXISTS` alone and would double-write under a
genuinely concurrent tick; nothing schedules one today, and that is a gap
worth naming rather than a claim worth repeating.
"""
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_sessionmaker
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.hooks import wire as wire_hooks
from rova.notifications.router import emit as _emit
from rova.domain.machines.registry import MACHINES


def housekeeping() -> None:
    session = get_sessionmaker()()
    try:
        session.execute(text("DELETE FROM idempotency_key WHERE created_at < :cutoff"),
                         {"cutoff": now().replace(hour=0, minute=0, second=0, microsecond=0)})
        session.execute(text("UPDATE auth_session SET revoked_at=:n WHERE expires_at < :n AND revoked_at IS NULL"),
                         {"n": now()})
        session.commit()
    finally:
        session.close()


def _fire_expired_timers(session: Session, *, policy_type: str, machine_code: str, trigger: str) -> int:
    """Generic SLA-timer-expiry driver. Every RUNNING `sla_timer` row of
    `policy_type` whose `expires_at` has passed is claimed (marked EXPIRED,
    guarded by `WHERE status='RUNNING'` so a timer the machine's own effects
    already cancelled — e.g. SM-02 SUBMIT racing this job — is silently
    skipped) and, only for rows actually claimed, `trigger` is applied on
    `machine_code` as SYSTEM. An `ApiError` from `apply` (the subject moved
    on by some other path in the same instant) is swallowed: the timer's
    EXPIRED mark is still correct either way, and this function's contract
    is "at most one attempt per expired timer", not "the transition always
    succeeds"."""
    machine = MACHINES[machine_code]
    due = session.execute(
        text("SELECT id, subject_id FROM sla_timer WHERE policy_type=:pt AND status='RUNNING' AND expires_at <= :now"),
        {"pt": policy_type, "now": now()},
    ).mappings().all()
    fired = 0
    for row in due:
        claimed = session.execute(
            text("UPDATE sla_timer SET status='EXPIRED', updated_at=:n WHERE id=:id AND status='RUNNING'"),
            {"n": now(), "id": row["id"]},
        )
        if claimed.rowcount == 0:
            continue
        try:
            machine.apply(session, row["subject_id"], trigger, "SYSTEM")
            fired += 1
        except ApiError:
            pass
    return fired


def _run_timer_job(policy_type: str, machine_code: str, trigger: str) -> int:
    wire_hooks()
    session = get_sessionmaker()()
    try:
        fired = _fire_expired_timers(session, policy_type=policy_type, machine_code=machine_code, trigger=trigger)
        session.commit()
        return fired
    finally:
        session.close()


def quotation_sla() -> int:
    return _run_timer_job("QUOTATION", "SM-02", "QUOTE_TIMEOUT")


def acceptance_sla() -> int:
    """A4.2/A14.6: the ACCEPTANCE timer's expiry trigger is gated on the
    vendor's `acceptance_mode` — `AUTO_ACCEPT_FULL` (R-128) fully confirms
    every line through SM-04 `CONFIRM_FULL` (never a raw write) and then
    fires SM-03 `AUTO_ACCEPT`; `MANUAL_CONFIRM` is the only mode that still
    means the vendor missed its window, so only it gets `ACCEPTANCE_TIMEOUT`.
    Item-2 fix (backend-review-r1.md): previously every mode got
    `ACCEPTANCE_TIMEOUT`, silently cancelling `AUTO_ACCEPT_FULL` vendors'
    orders on their own acceptance SLA."""
    wire_hooks()
    session = get_sessionmaker()()
    try:
        due = session.execute(
            text("SELECT id, subject_id FROM sla_timer WHERE policy_type='ACCEPTANCE' AND status='RUNNING' "
                 "AND expires_at <= :now"),
            {"now": now()},
        ).mappings().all()
        fired = 0
        for row in due:
            claimed = session.execute(
                text("UPDATE sla_timer SET status='EXPIRED', updated_at=:n WHERE id=:id AND status='RUNNING'"),
                {"n": now(), "id": row["id"]},
            )
            if claimed.rowcount == 0:
                continue
            order_id = row["subject_id"]
            order = session.execute(
                text('SELECT vendor_id, status FROM "order" WHERE id=:o'), {"o": order_id}
            ).mappings().first()
            if order is None or order["status"] != "PENDING_ACCEPTANCE":
                continue
            acceptance_mode = session.execute(
                text("SELECT acceptance_mode FROM vendor_account WHERE id=:v"), {"v": order["vendor_id"]}
            ).scalar()
            try:
                if acceptance_mode == "AUTO_ACCEPT_FULL":
                    lines = session.execute(
                        text("SELECT id FROM order_line WHERE order_id=:o"), {"o": order_id}
                    ).mappings().all()
                    for line in lines:
                        MACHINES["SM-04"].apply(session, line["id"], "CONFIRM_FULL", "SYSTEM")
                    MACHINES["SM-03"].apply(session, order_id, "AUTO_ACCEPT", "SYSTEM")
                else:
                    MACHINES["SM-03"].apply(session, order_id, "ACCEPTANCE_TIMEOUT", "SYSTEM")
                fired += 1
            except ApiError:
                pass
        session.commit()
        return fired
    finally:
        session.close()


def dispatch_sla() -> int:
    return _run_timer_job("DISPATCH", "SM-03", "DISPATCH_TIMEOUT")


def reroute_ask_timeout() -> int:
    return _run_timer_job("REROUTE_ASK", "SM-04", "DROP")


def dispute_resolution_sla() -> int:
    return _run_timer_job("DISPUTE_RESOLUTION", "SM-09", "ESCALATE")


def dispute_assignment() -> int:
    """R-124: an OPEN dispute with no assigned reviewer for longer than
    CFG-DISPUTE-ASSIGNMENT-HOURS is force-assigned to ComplianceOfficer by
    SYSTEM (SM-09 FORCE_ASSIGN), which itself starts SlaTimer(dispute_resolution)
    (see sm09_dispute.py's `_effect_start_resolution_timer`)."""
    wire_hooks()
    session = get_sessionmaker()()
    try:
        machine = MACHINES["SM-09"]
        hours = cfg.get(session, "CFG-DISPUTE-ASSIGNMENT-HOURS", default=24)
        cutoff = now() - timedelta(hours=float(hours))
        due = session.execute(
            text("SELECT id FROM dispute WHERE status='OPEN' AND created_at <= :cutoff FOR UPDATE SKIP LOCKED"),
            {"cutoff": cutoff},
        ).mappings().all()
        fired = 0
        for row in due:
            try:
                machine.apply(session, row["id"], "FORCE_ASSIGN", "SYSTEM")
                fired += 1
            except ApiError:
                pass
        session.commit()
        return fired
    finally:
        session.close()


def verification_sla() -> int:
    """CFG-VERIFICATION-SLA-HOURS (A-140): a vendor_account/pharmacy_account
    stuck ONBOARDING past this many hours since it entered that state gets an
    escalation notice queued for ComplianceOfficer — SM-06/SM-07 have no
    ONBOARDING-timeout state (out of Scope A), so there is no transition to
    fire; this is the notification-only reduction documented at module top.
    Each (holder, cutoff-window) pair is queued at most once by keying off
    the most recent already-queued VERIFICATION_SLA_BREACHED row for that
    subject, so a re-run before the underlying decision is made does not
    spam the outbox every tick."""
    wire_hooks()
    session = get_sessionmaker()()
    try:
        hours = cfg.get(session, "CFG-VERIFICATION-SLA-HOURS", default=120)
        cutoff = now() - timedelta(hours=float(hours))
        queued = 0
        for table, holder_type in (("vendor_account", "VENDOR"), ("pharmacy_account", "PHARMACY")):
            rows = session.execute(
                text(
                    f'SELECT id, created_at FROM "{table}" WHERE status=\'ONBOARDING\' AND created_at <= :cutoff'
                ),
                {"cutoff": cutoff},
            ).mappings().all()
            for row in rows:
                already = session.execute(
                    text(
                        "SELECT 1 FROM notification WHERE event_code='N-VERIFICATION_SLA_BREACHED' "
                        "AND payload->>'holder_id' = :hid"
                    ),
                    {"hid": row["id"]},
                ).first()
                if already:
                    continue
                _emit(
                    session, event_code="N-VERIFICATION_SLA_BREACHED",
                    recipient_role="ComplianceOfficer",
                    payload={"holder_type": holder_type, "holder_id": row["id"],
                             "onboarding_since": row["created_at"].isoformat()},
                )
                queued += 1
        session.commit()
        return queued
    finally:
        session.close()


def invoice_upload_sla() -> int:
    """CFG-INVOICE-UPLOAD-SLA-HOURS (A-143, PER_VENDOR): an order sitting
    RECEIPT_ACCEPTED with no invoice uploaded past this many hours since
    receipt acceptance gets an escalation notice queued for VendorFinance —
    invoice creation is a raw INSERT into `invoice` outside this codebase's
    machine layer (A8.1-style), so there is no SM-11 state to be "late" in;
    the escalation is the only spec-named effect (§16: "قبل التصعيد")."""
    wire_hooks()
    session = get_sessionmaker()()
    try:
        rows = session.execute(
            text(
                "SELECT o.id AS order_id, o.vendor_id, st.occurred_at FROM \"order\" o "
                "JOIN LATERAL (SELECT occurred_at FROM state_transition "
                "  WHERE machine='SM-03' AND subject_id = o.id AND to_state='RECEIPT_ACCEPTED' "
                "  ORDER BY occurred_at DESC LIMIT 1) st ON true "
                "WHERE o.status = 'RECEIPT_ACCEPTED' "
                "AND NOT EXISTS (SELECT 1 FROM invoice i WHERE i.order_id = o.id)"
            )
        ).mappings().all()
        queued = 0
        for row in rows:
            hours = cfg.get(session, "CFG-INVOICE-UPLOAD-SLA-HOURS", vendor_id=row["vendor_id"], default=72)
            if row["occurred_at"] + timedelta(hours=float(hours)) > now():
                continue
            already = session.execute(
                text(
                    "SELECT 1 FROM notification WHERE event_code='N-INVOICE_UPLOAD_SLA_BREACHED' "
                    "AND payload->>'order_id' = :oid"
                ),
                {"oid": row["order_id"]},
            ).first()
            if already:
                continue
            _emit(
                session, event_code="N-INVOICE_UPLOAD_SLA_BREACHED",
                recipient_role="VendorFinance",
                payload={"order_id": row["order_id"], "vendor_id": row["vendor_id"]},
            )
            queued += 1
        session.commit()
        return queued
    finally:
        session.close()


def cart_idle() -> int:
    """CFG-CART-IDLE-HOURS: a basket left untouched gets one notice, once.

    "Untouched" is the later of `request.updated_at` and the newest
    `request_line.updated_at`. Every cart write moves one or the other and a
    read moves neither, so this measures when the basket last *changed* —
    not when it was last looked at, which is a weaker reason to interrupt
    somebody.

    Addressed to the person who built the cart and to the pharmacy's
    organisation, because the basket belongs to the shop: the buyer who
    filled it should hear about it, and the admin who would have to approve
    it should be able to see it.

    Four things are deliberately not notified. An empty cart, because there
    is nothing to come back to. A cart that has already had its notice —
    `uq_one_idle_notice_per_cart` makes that the database's rule rather than
    this function's, so two overlapping ticks still produce one notice.
    Anything that is no longer an open cart, which is `is_cart AND
    status='DRAFT'` and nothing looser: a checked-out request keeps `is_cart`
    as provenance and must never be nagged about. And nothing at all on a
    channel this build cannot deliver — `emit` defaults to IN_APP and a
    WhatsApp row queued "for later" would sit QUEUED for ever while reading
    as a backlog somebody is working through.

    A notice also stops being true when the basket stops being one, which is
    not this job's business: `on_cart_left_draft` in `ordering/cart.py` marks
    it read on every SM-01 route out of DRAFT.
    """
    wire_hooks()
    session = get_sessionmaker()()
    try:
        hours = cfg.get(session, "CFG-CART-IDLE-HOURS", default=24)
        cutoff = now() - timedelta(hours=float(hours))
        rows = session.execute(
            text(
                "SELECT r.id AS request_id, r.number, r.pharmacy_id, r.created_by_user_id, "
                "       p.organisation_id, "
                "       GREATEST(r.updated_at, COALESCE(max(rl.updated_at), r.updated_at)) "
                "           AS last_touched_at, "
                "       count(*) AS line_count "
                "FROM request r "
                "JOIN pharmacy_account p ON p.id = r.pharmacy_id "
                "JOIN request_line rl ON rl.request_id = r.id "
                "WHERE r.is_cart AND r.status = 'DRAFT' "
                "GROUP BY r.id, r.number, r.pharmacy_id, r.created_by_user_id, p.organisation_id "
                "HAVING GREATEST(r.updated_at, COALESCE(max(rl.updated_at), r.updated_at)) <= :cutoff"
            ),
            {"cutoff": cutoff},
        ).mappings().all()

        queued = 0
        for row in rows:
            already = session.execute(
                text("SELECT 1 FROM notification WHERE event_code='N-CART-IDLE' "
                     "AND payload->>'request_id' = :rid"),
                {"rid": row["request_id"]},
            ).first()
            if already:
                continue

            # The first few product names, so the notice says what is in the
            # basket rather than only how much of it there is. Portuguese as
            # the catalogue holds it — a medicine name is not translated.
            names = session.execute(
                text("SELECT COALESCE(p.brand_name, p.inn) AS name FROM request_line rl "
                     "JOIN index_product p ON p.id = rl.index_product_id "
                     "WHERE rl.request_id = :r ORDER BY rl.created_at LIMIT 3"),
                {"r": row["request_id"]},
            ).scalars().all()

            # A SAVEPOINT around the insert: if a concurrent tick got there
            # between the check above and here, the unique index refuses this
            # one and the loop moves on instead of losing the whole run.
            try:
                with session.begin_nested():
                    _queue_idle_notice(session, row, names)
            except IntegrityError:
                continue
            queued += 1
        session.commit()
        return queued
    finally:
        session.close()


def _queue_idle_notice(session: Session, row, names: list[str]) -> None:
    _emit(
        session, event_code="N-CART-IDLE",
        recipient_user_id=row["created_by_user_id"],
        recipient_org_id=row["organisation_id"],
        payload={"request_id": row["request_id"], "number": row["number"],
                 "pharmacy_id": row["pharmacy_id"],
                 "line_count": row["line_count"],
                 "first_items": names,
                 # ISO, not str(datetime): the app parses this, and Python's
                 # default rendering ("… 10:00:00+00:00", with a space) is
                 # accepted by Chrome and rejected by Safari.
                 "last_touched_at": row["last_touched_at"].isoformat()},
    )


def admin_approval_timeout() -> int:
    """CFG-ADMIN-APPROVAL-TIMEOUT-HOURS (A-142): a basket waiting on the
    pharmacy admin that nobody answered goes back to the buyer.

    `ADMIN_REJECT` rather than a cancellation: the basket returns to DRAFT
    with its lines intact, which is where the pharmacist can edit it, split
    it under the threshold, or ask again. Silence from the admin is not the
    pharmacy deciding it does not want the medicines.

    Without this the timer expires, `_guard_admin_approve` starts refusing —
    it requires a running timer — and the request sits in
    AWAITING_ADMIN_APPROVAL where nobody can approve it and no screen can
    edit it. That is the same dead end the approval gate was wired to
    remove, one state further along.
    """
    wire_hooks()
    session = get_sessionmaker()()
    try:
        due = session.execute(
            text("SELECT id, subject_id FROM sla_timer WHERE policy_type='ADMIN_APPROVAL' "
                 "AND status='RUNNING' AND expires_at <= :now"),
            {"now": now()},
        ).mappings().all()
        fired = 0
        for row in due:
            claimed = session.execute(
                text("UPDATE sla_timer SET status='EXPIRED', updated_at=:n "
                     "WHERE id=:id AND status='RUNNING'"),
                {"n": now(), "id": row["id"]},
            ).rowcount
            if not claimed:
                continue
            request = session.execute(
                text("SELECT id, number, pharmacy_id, created_by_user_id, status "
                     "FROM request WHERE id=:r"), {"r": row["subject_id"]},
            ).mappings().first()
            if request is None or request["status"] != "AWAITING_ADMIN_APPROVAL":
                continue
            MACHINES["SM-01"].apply(session, request["id"], "ADMIN_REJECT", "SYSTEM")
            org = session.execute(
                text("SELECT organisation_id FROM pharmacy_account WHERE id=:p"),
                {"p": request["pharmacy_id"]}).scalar()
            _emit(session, event_code="N-CART-APPROVAL-TIMED-OUT",
                  recipient_user_id=request["created_by_user_id"],
                  recipient_org_id=org,
                  payload={"request_id": request["id"], "number": request["number"],
                           "pharmacy_id": request["pharmacy_id"]})
            fired += 1
        session.commit()
        return fired
    finally:
        session.close()


def close_orders() -> int:
    """A14.6 job 5 (item 2, backend-review-r1.md): `RECEIPT_ACCEPTED` orders
    past `CFG-RETURN-WINDOW-DAYS` attempt SM-03 `CLOSE` — its own guard
    (R-126) is the single source of truth for whether they are actually
    eligible (a price-matched invoice, settled or on credit terms), so an
    order that fails the guard is simply skipped this tick, same pattern as
    `_fire_expired_timers` above. The invoice-upload-SLA half of this job is
    `invoice_upload_sla` above (unchanged)."""
    wire_hooks()
    session = get_sessionmaker()()
    try:
        days = cfg.get(session, "CFG-RETURN-WINDOW-DAYS", default=7)
        cutoff = now() - timedelta(days=float(days))
        rows = session.execute(
            text(
                "SELECT o.id FROM \"order\" o JOIN LATERAL ("
                "  SELECT occurred_at FROM state_transition WHERE machine='SM-03' AND subject_id=o.id "
                "  AND to_state='RECEIPT_ACCEPTED' ORDER BY occurred_at DESC LIMIT 1"
                ") st ON true "
                "WHERE o.status='RECEIPT_ACCEPTED' AND st.occurred_at <= :cutoff"
            ),
            {"cutoff": cutoff},
        ).mappings().all()
        closed = 0
        for row in rows:
            try:
                MACHINES["SM-03"].apply(session, row["id"], "CLOSE", "SYSTEM")
                closed += 1
            except ApiError:
                pass
        session.commit()
        return closed
    finally:
        session.close()


JOBS = {
    "housekeeping": housekeeping,
    "quotation_sla": quotation_sla,
    "acceptance_sla": acceptance_sla,
    "dispatch_sla": dispatch_sla,
    "reroute_ask_timeout": reroute_ask_timeout,
    "dispute_assignment": dispute_assignment,
    "dispute_resolution_sla": dispute_resolution_sla,
    "verification_sla": verification_sla,
    "invoice_upload_sla": invoice_upload_sla,
    "cart_idle": cart_idle,
    "admin_approval_timeout": admin_approval_timeout,
    "close_orders": close_orders,
}


def tick() -> None:
    wire_hooks()
    for name, fn in JOBS.items():
        fn()


def run(name: str) -> None:
    wire_hooks()
    JOBS[name]()
