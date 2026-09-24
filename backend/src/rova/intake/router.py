"""Multi-channel intake — the product promise that an order arrives in
whatever shape the pharmacist already works in.

Nothing here invents a second request lifecycle. Every channel lands on the
same `request` row and the same SM-01 machine; what differs is only how the
raw lines are obtained:

    text / whatsapp / pms / file   the vendored matcher resolves them now
    photo / voice / phone          a human transcribes them from the ops queue

A channel that can resolve lines immediately creates the request in
`NORMALIZING` and runs the real `rova.matching` ladder — the same code path
the price-list importer uses, never a second matching implementation. A
channel that cannot leaves the request in `NORMALIZING` with the raw artefact
stored and zero lines, which is precisely what the ops transcription queue
below reads.

`NORMALIZATION_COMPLETE` is refused while any line is still `ASK` or
`UNRESOLVED`: the whole point of the matcher's three-state output is that a
line nobody resolved never silently becomes an order line.
"""
import json
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.idempotency import check_and_store, store
from rova.core.ids import new_id
from rova.core.storage import save_bytes
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES
from rova.matching.adapter import match_lines

router = APIRouter(prefix="/v1", tags=["intake"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)
_PHARMACY_OR_OPS = _PHARMACY + (RoleCode.OPS_REVIEWER,)
_OPS = (RoleCode.OPS_REVIEWER, RoleCode.SUPPORT_AGENT, RoleCode.PLATFORM_ADMIN)

Channel = Literal["APP", "WHATSAPP_TEXT", "VOICE_NOTE", "PHOTO", "FILE", "PHONE_CALL", "PMS"]
Mode = Literal["CATALOGUE", "RFQ", "ASSISTED"]
AllocationStrategy = Literal["FEWEST_VENDORS", "FASTEST_DISPATCH", "BEST_TERMS"]

# the channels whose raw artefact a human must read before there are lines
_HUMAN_TRANSCRIBED = ("VOICE_NOTE", "PHOTO", "PHONE_CALL")
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _next_number(session: Session) -> str:
    import datetime
    seq = session.execute(text("SELECT nextval('request_number_seq')")).scalar()
    return f"RQ-{datetime.date.today().year}-{str(seq).zfill(6)}"


def _pharmacy_id_for(principal: Principal, explicit: str | None, session: Session) -> str:
    """A pharmacy principal always acts for its own pharmacy. An ops principal
    (the phone-call and assisted channels) must name the pharmacy it is acting
    for, and that pharmacy must exist."""
    if principal.pharmacy_id:
        if explicit and explicit != principal.pharmacy_id:
            raise ApiError("FORBIDDEN", "cannot raise a request for another pharmacy")
        return principal.pharmacy_id
    if not explicit:
        raise ApiError("VALIDATION_ERROR", "pharmacy_id is required when acting on a pharmacy's behalf")
    if not session.execute(text("SELECT 1 FROM pharmacy_account WHERE id=:p"), {"p": explicit}).first():
        raise ApiError("NOT_FOUND", "pharmacy not found")
    return explicit


def _create_request(session: Session, principal: Principal, *, pharmacy_id: str, channel: str,
                    mode: str, strategy: str, raw_payload: dict,
                    raw_payload_ref: str | None = None,
                    pms_idempotency_key: str | None = None) -> dict:
    rid = new_id("req")
    number = _next_number(session)
    session.execute(
        text("INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy, "
             "created_by_user_id, acting_ops_user_id, raw_payload, raw_payload_ref, pms_idempotency_key) "
             "VALUES (:id, :num, :ph, :mode, :ch, 'NORMALIZING', :strat, :uid, :ops, "
             "CAST(:raw AS JSONB), :ref, :pms)"),
        {"id": rid, "num": number, "ph": pharmacy_id, "mode": mode, "ch": channel, "strat": strategy,
         "uid": principal.user_id if principal.pharmacy_id else None,
         "ops": None if principal.pharmacy_id else principal.user_id,
         "raw": json.dumps(raw_payload), "ref": raw_payload_ref, "pms": pms_idempotency_key},
    )
    return {"id": rid, "number": number, "status": "NORMALIZING", "channel": channel}


def _insert_matched_lines(session: Session, request_id: str, raw_lines: list[str],
                          quantities: list[int] | None = None) -> list[dict]:
    """Runs the vendored matcher and writes one `request_line` per raw line,
    carrying the matcher's own verdict forward untouched. A line the matcher
    could not resolve is stored with `index_product_id = NULL` and its
    candidate list — never guessed into a product."""
    if not raw_lines:
        return []
    results = match_lines(session, raw_lines)
    out = []
    for i, m in enumerate(results):
        qty = quantities[i] if quantities and i < len(quantities) else 1
        if qty <= 0:
            raise ApiError("VALIDATION_ERROR", "quantity must be > 0",
                           details=[{"field": f"lines.{i}.quantity", "reason": "must be > 0"}])
        line_id = new_id("rql")
        session.execute(
            text("INSERT INTO request_line (id, request_id, index_product_id, qty_requested, line_kind, "
                 "match_status, confidence, source_span, candidate_product_ids) "
                 "VALUES (:id, :r, :p, :q, 'CATALOGUE', :ms, :cf, :src, CAST(:cands AS JSONB))"),
            {"id": line_id, "r": request_id, "p": m.index_product_id, "q": qty,
             "ms": m.match_status, "cf": m.confidence, "src": m.raw,
             "cands": json.dumps(m.candidates)},
        )
        out.append({"id": line_id, "raw": m.raw, "qty_requested": qty,
                    "index_product_id": m.index_product_id, "match_status": m.match_status,
                    "confidence": m.confidence, "candidate_product_ids": m.candidates})
    return out


def _request_or_404(session: Session, request_id: str, principal: Principal) -> dict:
    row = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": request_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "request not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "request not found")
    return dict(row)


# ------------------------------------------------------------------ text-like

class RawLine(Body):
    text: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0)


class TextIntake(Body):
    pharmacy_id: str | None = None
    channel: Literal["APP", "WHATSAPP_TEXT"] = "WHATSAPP_TEXT"
    mode: Mode = "CATALOGUE"
    allocation_strategy: AllocationStrategy = "FEWEST_VENDORS"
    body: str | None = Field(default=None, max_length=20000)
    lines: list[RawLine] | None = None

    def raw_lines(self) -> tuple[list[str], list[int]]:
        if self.lines:
            return [l.text for l in self.lines], [l.quantity for l in self.lines]
        if self.body:
            parsed = [l.strip() for l in self.body.splitlines() if l.strip()]
            return parsed, [1] * len(parsed)
        return [], []


@router.post("/intake/text", status_code=201)
def intake_text(body: TextIntake,
                principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                session: Session = Depends(get_session, scope="function")):
    """A pasted WhatsApp list, or a structured line array from the app."""
    raws, qtys = body.raw_lines()
    if not raws:
        raise ApiError("VALIDATION_ERROR", "either body or lines must contain at least one line")
    pharmacy_id = _pharmacy_id_for(principal, body.pharmacy_id, session)
    req = _create_request(session, principal, pharmacy_id=pharmacy_id, channel=body.channel,
                          mode=body.mode, strategy=body.allocation_strategy,
                          raw_payload={"body": body.body, "line_count": len(raws)})
    lines = _insert_matched_lines(session, req["id"], raws, qtys)
    return {**req, "lines": lines, "unresolved_count": sum(1 for l in lines
                                                            if l["match_status"] in ("ASK", "UNRESOLVED"))}


@router.post("/intake/file", status_code=201)
async def intake_file(file: UploadFile = File(...),
                      pharmacy_id: str | None = Form(default=None),
                      mode: Mode = Form(default="CATALOGUE"),
                      allocation_strategy: AllocationStrategy = Form(default="FEWEST_VENDORS"),
                      principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                      session: Session = Depends(get_session, scope="function")):
    """A plain-text or CSV order list. One raw line per file line; a CSV's
    first two columns are read as (name, quantity) when the second parses as
    a positive integer, otherwise the whole line is treated as the name."""
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ApiError("VALIDATION_ERROR", "file exceeds the 20 MB upload limit")
    if not data:
        raise ApiError("VALIDATION_ERROR", "file is empty")
    ref = save_bytes("intake", file.filename or "order.txt", data)

    raws: list[str] = []
    qtys: list[int] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
            raws.append(parts[0])
            qtys.append(int(parts[1]))
        else:
            raws.append(line)
            qtys.append(1)
    if not raws:
        raise ApiError("VALIDATION_ERROR", "file contains no readable lines")

    pid = _pharmacy_id_for(principal, pharmacy_id, session)
    req = _create_request(session, principal, pharmacy_id=pid, channel="FILE", mode=mode,
                          strategy=allocation_strategy,
                          raw_payload={"filename": file.filename, "bytes": len(data)},
                          raw_payload_ref=ref)
    lines = _insert_matched_lines(session, req["id"], raws, qtys)
    return {**req, "raw_payload_ref": ref, "lines": lines,
            "unresolved_count": sum(1 for l in lines if l["match_status"] in ("ASK", "UNRESOLVED"))}


# ------------------------------------------------------------------ artefact channels

async def _artefact_intake(session, principal, *, channel: str, file: UploadFile,
                           pharmacy_id: str | None, mode: str, strategy: str) -> dict:
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ApiError("VALIDATION_ERROR", "file exceeds the 20 MB upload limit")
    if not data:
        raise ApiError("VALIDATION_ERROR", "file is empty")
    ref = save_bytes(f"intake/{channel.lower()}", file.filename or channel.lower(), data)
    pid = _pharmacy_id_for(principal, pharmacy_id, session)
    req = _create_request(session, principal, pharmacy_id=pid, channel=channel, mode=mode,
                          strategy=strategy,
                          raw_payload={"filename": file.filename, "bytes": len(data),
                                       "content_type": file.content_type},
                          raw_payload_ref=ref)
    return {**req, "raw_payload_ref": ref, "lines": [],
            "awaiting_transcription": True,
            "note_pt": "Recebido. A sua lista está a ser lida pela nossa equipa."}


@router.post("/intake/photo", status_code=201)
async def intake_photo(file: UploadFile = File(...),
                       pharmacy_id: str | None = Form(default=None),
                       mode: Mode = Form(default="CATALOGUE"),
                       allocation_strategy: AllocationStrategy = Form(default="FEWEST_VENDORS"),
                       principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                       session: Session = Depends(get_session, scope="function")):
    """A photo of a handwritten list. Stored and queued; never OCR-guessed
    into order lines without a human."""
    return await _artefact_intake(session, principal, channel="PHOTO", file=file,
                                  pharmacy_id=pharmacy_id, mode=mode, strategy=allocation_strategy)


@router.post("/intake/voice", status_code=201)
async def intake_voice(file: UploadFile = File(...),
                       pharmacy_id: str | None = Form(default=None),
                       mode: Mode = Form(default="CATALOGUE"),
                       allocation_strategy: AllocationStrategy = Form(default="FEWEST_VENDORS"),
                       principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                       session: Session = Depends(get_session, scope="function")):
    """A WhatsApp voice note."""
    return await _artefact_intake(session, principal, channel="VOICE_NOTE", file=file,
                                  pharmacy_id=pharmacy_id, mode=mode, strategy=allocation_strategy)


class PhoneCallIntake(Body):
    pharmacy_id: str
    mode: Mode = "ASSISTED"
    allocation_strategy: AllocationStrategy = "FEWEST_VENDORS"
    call_summary: str = Field(min_length=1, max_length=5000)
    lines: list[RawLine] | None = None


@router.post("/intake/phone-call", status_code=201)
def intake_phone_call(body: PhoneCallIntake,
                      principal: Principal = Depends(require_roles(*_OPS)),
                      session: Session = Depends(get_session, scope="function")):
    """An ops agent taking an order over the phone. The agent is recorded as
    `acting_ops_user_id`, never as the pharmacy's own user."""
    if principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "the phone-call channel is an ops action")
    pid = _pharmacy_id_for(principal, body.pharmacy_id, session)
    req = _create_request(session, principal, pharmacy_id=pid, channel="PHONE_CALL", mode=body.mode,
                          strategy=body.allocation_strategy,
                          raw_payload={"call_summary": body.call_summary})
    lines = []
    if body.lines:
        lines = _insert_matched_lines(session, req["id"], [l.text for l in body.lines],
                                      [l.quantity for l in body.lines])
    return {**req, "lines": lines, "awaiting_transcription": not lines}


# ------------------------------------------------------------------ PMS

class PmsIntake(Body):
    pharmacy_id: str | None = None
    mode: Mode = "CATALOGUE"
    allocation_strategy: AllocationStrategy = "FEWEST_VENDORS"
    lines: list[RawLine] = Field(min_length=1)


@router.post("/intake/pms", status_code=201)
def intake_pms(body: PmsIntake,
               principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
               session: Session = Depends(get_session, scope="function"),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """A push from the pharmacy's own management system.

    A PMS retries. The idempotency key is mandatory here and is hashed
    together with the request body, so the same key with a different basket
    is a 409 rather than a silent replay of the first basket — the defect
    fixed in `core/idempotency.py` and enforced at this call site by passing
    the real body, not a stand-in.
    """
    pid = _pharmacy_id_for(principal, body.pharmacy_id, session)
    principal_id = principal.membership_id or principal.user_id
    body_dict = body.model_dump()
    replay = check_and_store(session, key=idempotency_key, principal_id=principal_id,
                             method="POST", path="/v1/intake/pms", body=body_dict)
    if replay is not None:
        return replay["body"]

    req = _create_request(session, principal, pharmacy_id=pid, channel="PMS", mode=body.mode,
                          strategy=body.allocation_strategy,
                          raw_payload={"line_count": len(body.lines)},
                          pms_idempotency_key=idempotency_key)
    lines = _insert_matched_lines(session, req["id"], [l.text for l in body.lines],
                                  [l.quantity for l in body.lines])
    result = {**req, "lines": lines,
              "unresolved_count": sum(1 for l in lines if l["match_status"] in ("ASK", "UNRESOLVED"))}
    store(session, key=idempotency_key, principal_id=principal_id, method="POST",
          path="/v1/intake/pms", body=body_dict, status_code=201, response_body=result)
    return result


# ------------------------------------------------------------------ normalisation

@router.get("/intake/queue")
def transcription_queue(limit: int = 50,
                        principal: Principal = Depends(require_roles(*_OPS)),
                        session: Session = Depends(get_session, scope="function")):
    """Every request whose artefact still needs a human to read it: a photo,
    a voice note or a phone call that arrived with no lines."""
    rows = session.execute(
        text("SELECT r.*, (SELECT count(*) FROM request_line rl WHERE rl.request_id = r.id) AS line_count "
             "FROM request r WHERE r.status='NORMALIZING' AND r.channel = ANY(:ch) "
             "ORDER BY r.created_at ASC LIMIT :lim"),
        {"ch": list(_HUMAN_TRANSCRIBED), "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows if r["line_count"] == 0]}


class Transcription(Body):
    lines: list[RawLine] = Field(min_length=1)


@router.post("/intake/{request_id}/transcribe")
def transcribe(request_id: str, body: Transcription,
               principal: Principal = Depends(require_roles(*_OPS)),
               session: Session = Depends(get_session, scope="function")):
    """An ops agent reads the stored photo / voice note and types the lines.
    They still go through the matcher — a human reads handwriting, the
    matcher resolves the product."""
    req = _request_or_404(session, request_id, principal)
    if req["status"] != "NORMALIZING":
        raise ApiError("ILLEGAL_TRANSITION", "request is not in NORMALIZING")
    existing = session.execute(text("SELECT count(*) FROM request_line WHERE request_id=:r"),
                               {"r": request_id}).scalar()
    if existing:
        raise ApiError("CONFLICT", "this request already has lines")
    session.execute(text("UPDATE request SET acting_ops_user_id=:u, updated_at=:t WHERE id=:r"),
                    {"u": principal.user_id, "t": now(), "r": request_id})
    lines = _insert_matched_lines(session, request_id, [l.text for l in body.lines],
                                  [l.quantity for l in body.lines])
    return {"request_id": request_id, "lines": lines,
            "unresolved_count": sum(1 for l in lines if l["match_status"] in ("ASK", "UNRESOLVED"))}


@router.get("/intake/{request_id}")
def normalisation_view(request_id: str,
                       principal: Principal = Depends(require_roles(*(_PHARMACY_OR_OPS + _OPS))),
                       session: Session = Depends(get_session, scope="function")):
    """What the confirmation screen renders: every line with the matcher's
    verdict, and for an `ASK` line the candidate products spelled out so the
    pharmacist chooses rather than trusts."""
    req = _request_or_404(session, request_id, principal)
    lines = session.execute(
        text("SELECT * FROM request_line WHERE request_id=:r ORDER BY created_at"), {"r": request_id}
    ).mappings().all()
    out = []
    for line in lines:
        candidates = line["candidate_product_ids"] or []
        detail = []
        if candidates:
            detail = [dict(r) for r in session.execute(
                text("SELECT id, inn, brand_name, form, strength, pack_size FROM index_product "
                     "WHERE id = ANY(:ids)"), {"ids": list(candidates)},
            ).mappings().all()]
        out.append({**dict(line), "candidates": detail})
    blocking = [l["id"] for l in lines if l["match_status"] in ("ASK", "UNRESOLVED")]
    return {"request": req, "lines": out, "blocking_line_ids": blocking,
            "can_complete_normalisation": not blocking}


class ResolveLine(Body):
    index_product_id: str


@router.post("/request-lines/{line_id}/resolve")
def resolve_line(line_id: str, body: ResolveLine,
                 principal: Principal = Depends(require_roles(*(_PHARMACY + _OPS))),
                 session: Session = Depends(get_session, scope="function")):
    """A human picks one of the candidates. The choice is recorded as
    `RESOLVED`, which is deliberately a different value from the matcher's
    own `AUTO` — so an audit can always tell a machine decision from a human
    one."""
    line = session.execute(text("SELECT * FROM request_line WHERE id=:id"), {"id": line_id}).mappings().first()
    if line is None:
        raise ApiError("NOT_FOUND", "request line not found")
    _request_or_404(session, line["request_id"], principal)
    if line["match_status"] not in ("ASK", "UNRESOLVED"):
        raise ApiError("ILLEGAL_TRANSITION", "line is already resolved")
    product = session.execute(
        text("SELECT id FROM index_product WHERE id=:p AND review_status='PUBLISHED'"),
        {"p": body.index_product_id},
    ).scalar()
    if product is None:
        raise ApiError("NOT_FOUND", "index product not found or not published")
    session.execute(
        text("UPDATE request_line SET index_product_id=:p, match_status='RESOLVED', updated_at=:t "
             "WHERE id=:id AND match_status IN ('ASK','UNRESOLVED')"),
        {"p": body.index_product_id, "t": now(), "id": line_id},
    )
    return dict(session.execute(text("SELECT * FROM request_line WHERE id=:id"),
                                {"id": line_id}).mappings().one())


@router.delete("/request-lines/{line_id}")
def drop_line(line_id: str,
              principal: Principal = Depends(require_roles(*(_PHARMACY + _OPS))),
              session: Session = Depends(get_session, scope="function")):
    """Dropping a line the pharmacist does not want. A line dropped because
    nothing in the Index matched is recorded as a demand gap first — that is
    the signal the Index is missing a product, and losing it loses the only
    evidence the catalogue needs to grow."""
    line = session.execute(text("SELECT * FROM request_line WHERE id=:id"), {"id": line_id}).mappings().first()
    if line is None:
        raise ApiError("NOT_FOUND", "request line not found")
    req = _request_or_404(session, line["request_id"], principal)
    if req["status"] not in ("NORMALIZING", "DRAFT", "AWAITING_CONFIRMATION"):
        raise ApiError("ILLEGAL_TRANSITION", "lines can only be dropped before the request is confirmed")
    if line["match_status"] == "UNRESOLVED" and line["index_product_id"] is None:
        region = session.execute(text("SELECT region_code FROM pharmacy_account WHERE id=:p"),
                                 {"p": req["pharmacy_id"]}).scalar()
        # `request_line_id` is deliberately left NULL: the line is about to be
        # deleted and `demand_gap.request_line_id` is a real FK, so storing it
        # would either block the delete or dangle. What the Index team needs is
        # the raw text, the quantity, the pharmacy and the region — all of
        # which survive here. The gap outlives the basket it came from.
        session.execute(
            text("INSERT INTO demand_gap (id, index_product_id, raw_product_text, pharmacy_id, region_code, "
                 "qty_requested, cause, request_line_id, occurred_at) "
                 "VALUES (:id, NULL, :raw, :ph, :rc, :q, 'PRODUCT_NOT_IN_INDEX', NULL, :t)"),
            {"id": new_id("dmg"), "raw": line["source_span"] or "", "ph": req["pharmacy_id"],
             "rc": region, "q": line["qty_requested"], "t": now()},
        )
    session.execute(text("DELETE FROM request_line WHERE id=:id"), {"id": line_id})
    # Removing a line is touching the cart. Without this the idle-cart job
    # would read a basket somebody just edited as one nobody has opened.
    session.execute(text("UPDATE request SET updated_at = now() WHERE id=:r"),
                    {"r": line["request_id"]})
    return {"deleted": line_id}


@router.post("/requests/{request_id}/normalization-complete")
def normalization_complete(request_id: str,
                           principal: Principal = Depends(require_roles(*(_PHARMACY + _OPS))),
                           session: Session = Depends(get_session, scope="function")):
    """SM-01 NORMALIZATION_COMPLETE — refused while any line is still
    unresolved, and refused on an empty basket.

    Open to the pharmacy itself, not only to ops. The pharmacy resolves its
    own `ASK` lines through `/request-lines/{id}/resolve`; requiring an ops
    agent to then press one more button would put a human in the loop on
    every single WhatsApp order for no added safety — the guard below, not
    the caller's role, is what actually protects the basket. The transition
    is applied as SYSTEM because that is the only actor SM-01 accepts for it.
    """
    _request_or_404(session, request_id, principal)
    rows = session.execute(
        text("SELECT match_status FROM request_line WHERE request_id=:r"), {"r": request_id}
    ).scalars().all()
    if not rows:
        raise ApiError("GUARD_FAILED", "the request has no lines")
    blocking = [s for s in rows if s in ("ASK", "UNRESOLVED")]
    if blocking:
        raise ApiError("GUARD_FAILED", f"{len(blocking)} line(s) still need a human decision")
    return dict(MACHINES["SM-01"].apply(session, request_id, "NORMALIZATION_COMPLETE", "SYSTEM"))


def _request_transition(trigger: str, roles):
    def _endpoint(request_id: str,
                  principal: Principal = Depends(require_roles(*roles)),
                  session: Session = Depends(get_session, scope="function")):
        _request_or_404(session, request_id, principal)
        return dict(MACHINES["SM-01"].apply(session, request_id, trigger, principal))
    return _endpoint


router.add_api_route("/requests/{request_id}/confirm",
                     _request_transition("CONFIRM", (RoleCode.PHARMACY_BUYER, RoleCode.OPS_REVIEWER)),
                     methods=["POST"], name="confirm_request", tags=["intake"])
router.add_api_route("/requests/{request_id}/request-changes",
                     _request_transition("REQUEST_CHANGES", (RoleCode.PHARMACY_BUYER, RoleCode.OPS_REVIEWER)),
                     methods=["POST"], name="request_changes", tags=["intake"])
router.add_api_route("/requests/{request_id}/submit",
                     _request_transition("SUBMIT_CART", (RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN,
                                                         RoleCode.OPS_REVIEWER)),
                     methods=["POST"], name="submit_request", tags=["intake"])
router.add_api_route("/requests/{request_id}/admin-approve",
                     _request_transition("ADMIN_APPROVE", (RoleCode.PHARMACY_ADMIN,)),
                     methods=["POST"], name="admin_approve_request", tags=["intake"])
router.add_api_route("/requests/{request_id}/admin-reject",
                     _request_transition("ADMIN_REJECT", (RoleCode.PHARMACY_ADMIN,)),
                     methods=["POST"], name="admin_reject_request", tags=["intake"])
router.add_api_route("/requests/{request_id}/abandon",
                     _request_transition("ABANDON", (RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN,
                                                     RoleCode.OPS_REVIEWER)),
                     methods=["POST"], name="abandon_request", tags=["intake"])
router.add_api_route("/requests/{request_id}/cancel",
                     _request_transition("CANCEL", (RoleCode.PHARMACY_ADMIN,)),
                     methods=["POST"], name="cancel_request", tags=["intake"])


@router.get("/requests")
def list_requests(status: str | None = None, channel: Channel | None = None,
                  pharmacy_id: str | None = None, limit: int = 50,
                  include_cart: bool = True,
                  principal: Principal = Depends(require_roles(*(_PHARMACY_OR_OPS + _OPS))),
                  session: Session = Depends(get_session, scope="function")):
    scope_pharmacy = principal.pharmacy_id or pharmacy_id
    rows = session.execute(
        text("SELECT r.*, (SELECT count(*) FROM request_line rl WHERE rl.request_id=r.id) AS line_count "
             "FROM request r WHERE (CAST(:ph AS TEXT) IS NULL OR r.pharmacy_id=:ph) "
             "AND (CAST(:st AS TEXT) IS NULL OR r.status=:st) "
             "AND (CAST(:ch AS TEXT) IS NULL OR r.channel=:ch) "
             # The cart is a DRAFT request. It belongs on the cart screen, not
             # in the order history, where it would read as an order the
             # pharmacist had sent. Default stays True so nothing that already
             # calls this changes shape.
             #
             # `is_cart AND status='DRAFT'`, the same definition the partial
             # unique index uses — not `is_cart` alone. The flag is never
             # cleared, on purpose: it is the provenance a re-order shelf is
             # built from. Excluding on the flag by itself hid every order the
             # pharmacy had ever placed from the app.
             "AND (:cart OR NOT (r.is_cart AND r.status='DRAFT')) "
             "ORDER BY r.created_at DESC LIMIT :lim"),
        {"ph": scope_pharmacy, "st": status, "ch": channel, "lim": limit,
         "cart": include_cart},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/requests/{request_id}/transitions")
def request_transitions(request_id: str,
                        principal: Principal = Depends(require_roles(*(_PHARMACY_OR_OPS + _OPS))),
                        session: Session = Depends(get_session, scope="function")):
    _request_or_404(session, request_id, principal)
    rows = session.execute(
        text("SELECT * FROM state_transition WHERE subject_type='request' AND subject_id=:r "
             "ORDER BY occurred_at ASC"), {"r": request_id},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


# ------------------------------------------------------------------ demand gaps

@router.get("/demand-gaps")
def list_demand_gaps(cause: str | None = None, region_code: str | None = None, limit: int = 100,
                     principal: Principal = Depends(require_roles(
                         RoleCode.OPS_REVIEWER, RoleCode.INDEX_PHARMACIST, RoleCode.PLATFORM_ADMIN)),
                     session: Session = Depends(get_session, scope="function")):
    """What the pharmacies asked for and the platform could not supply — the
    only evidence that says which product the Index is missing next."""
    rows = session.execute(
        text("SELECT * FROM demand_gap WHERE (CAST(:c AS TEXT) IS NULL OR cause=:c) "
             "AND (CAST(:r AS TEXT) IS NULL OR region_code=:r) ORDER BY occurred_at DESC LIMIT :lim"),
        {"c": cause, "r": region_code, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/demand-gaps/summary")
def demand_gap_summary(principal: Principal = Depends(require_roles(
                           RoleCode.OPS_REVIEWER, RoleCode.INDEX_PHARMACIST, RoleCode.PLATFORM_ADMIN)),
                       session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT cause, region_code, count(*) AS gaps, sum(qty_requested) AS units "
             "FROM demand_gap GROUP BY cause, region_code ORDER BY gaps DESC")
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}
