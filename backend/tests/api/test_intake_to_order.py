"""The WhatsApp lane has to end in an order, not at CONFIRMED.

Found against the live pilot server in September 2026: a pharmacy pasted a
WhatsApp list, resolved the matcher's question, confirmed the basket — and
`POST /v1/requests/{id}/checkout` answered

    409 GUARD_FAILED  "request is not in DRAFT"  rule SM-01

because `checkout()` accepted `DRAFT` only. Every existing test drove the
catalogue lane, which starts at `DRAFT`, so the whole intake lane was a dead
end that no test could see. The same hole closed the admin-approval lane:
`DRAFT -> AWAITING_ADMIN_APPROVAL -> CONFIRMED` could not buy either.

These tests drive the intake lane over HTTP only.
"""
from sqlalchemy import text

from tests.api.test_orders_flow import _login, _setup_fixture, _transitions


def _publish(db_engine, fx):
    """`_setup_fixture` leaves the index at its default review status; the
    matcher and `/resolve` only ever hand back a PUBLISHED product."""
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE index_product SET review_status='PUBLISHED' WHERE id IN (:a, :b)"),
                     {"a": fx["product_a"], "b": fx["product_b"]})


def _paste_and_confirm(client, db_engine, fx, headers, *, confirm=True) -> str:
    """WhatsApp paste -> resolve every blocking line -> normalisation -> CONFIRM."""
    rid = client.post(
        "/v1/intake/text", headers=headers,
        json={"lines": [{"text": "Drug a", "quantity": 3}]},
    ).json()["id"]

    view = client.get(f"/v1/intake/{rid}", headers=headers).json()
    for line_id in view["blocking_line_ids"]:
        r = client.post(f"/v1/request-lines/{line_id}/resolve", headers=headers,
                        json={"index_product_id": fx["product_a"]})
        assert r.status_code == 200, r.text

    assert client.post(f"/v1/requests/{rid}/normalization-complete", headers=headers).status_code == 200
    if confirm:
        assert client.post(f"/v1/requests/{rid}/confirm", headers=headers).status_code == 200
    return rid


def test_a_confirmed_whatsapp_basket_becomes_a_real_order(client, db_engine):
    fx = _setup_fixture(db_engine, "i2o1")
    _publish(db_engine, fx)
    h = _login(client, fx["pharmacy_phone"])
    rid = _paste_and_confirm(client, db_engine, fx, h)

    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**h, "Idempotency-Key": "i2o1-key"}, json={})
    assert r.status_code == 200, r.text

    orders = r.json()["orders"]
    assert len(orders) == 1, "one vendor in the fixture, so one order"
    assert orders[0]["goods_total"] == "60.00", "3 units at 20.00, as a 2dp string"

    with db_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM request WHERE id=:r"), {"r": rid}).scalar()
    assert status == "IN_FULFILMENT"

    # The lane is walked in full, and SUBMIT_CART — which belongs to the
    # catalogue lane — is never applied to a request that never was a cart.
    triggers = [t for _, t, _ in _transitions(db_engine, rid)]
    assert triggers == ["NORMALIZATION_COMPLETE", "CONFIRM", "FIRST_ORDER_CREATED"]


def test_checkout_before_the_buyer_confirms_is_refused(client, db_engine):
    """`AWAITING_CONFIRMATION` stays closed: confirming is the buyer's own
    act under SM-01, and checkout must not perform it for them."""
    fx = _setup_fixture(db_engine, "i2o2")
    _publish(db_engine, fx)
    h = _login(client, fx["pharmacy_phone"])
    rid = _paste_and_confirm(client, db_engine, fx, h, confirm=False)

    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**h, "Idempotency-Key": "i2o2-key"}, json={})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "GUARD_FAILED"
    assert "AWAITING_CONFIRMATION" in body["message"]
    assert body["rule"] == "SM-01"

    with db_engine.connect() as conn:
        n = conn.execute(text('SELECT count(*) FROM "order" WHERE request_id=:r'), {"r": rid}).scalar()
    assert n == 0, "a refused checkout must not leave a half-made order behind"


def test_a_second_checkout_of_the_same_request_is_refused(client, db_engine):
    """Once the request is `IN_FULFILMENT` it is neither `DRAFT` nor
    `CONFIRMED`, so widening the entry states did not open a way to buy the
    same basket twice under a fresh idempotency key."""
    fx = _setup_fixture(db_engine, "i2o3")
    _publish(db_engine, fx)
    h = _login(client, fx["pharmacy_phone"])
    rid = _paste_and_confirm(client, db_engine, fx, h)

    first = client.post(f"/v1/requests/{rid}/checkout",
                        headers={**h, "Idempotency-Key": "i2o3-a"}, json={})
    assert first.status_code == 200, first.text

    second = client.post(f"/v1/requests/{rid}/checkout",
                         headers={**h, "Idempotency-Key": "i2o3-b"}, json={})
    assert second.status_code == 409
    assert "IN_FULFILMENT" in second.json()["error"]["message"]

    with db_engine.connect() as conn:
        n = conn.execute(text('SELECT count(*) FROM "order" WHERE request_id=:r'), {"r": rid}).scalar()
    assert n == 1
