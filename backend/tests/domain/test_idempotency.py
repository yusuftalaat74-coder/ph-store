"""A15.3 / B fix 1c — direct, real-DB proof of `rova.core.idempotency`'s
conflict/replay behaviour with an ACTUAL differing request body, not a
different URL path standing in for one.

Why this lives here rather than only as an HTTP test: the one production
call site, `POST /v1/requests/{id}/checkout` in `rova/ordering/router.py`,
has no request-body model at all and always passes `body={}` to both
`check_and_store`/`store` regardless of what the client sends — so no HTTP
request against that endpoint can ever exercise a genuinely different body
(see `rova/core/idempotency.py`'s own module docstring, and
`tests/api/test_checkout_idempotency.py::test_same_key_different_body_conflicts`,
which documents the same limit rather than faking around it). That endpoint
sits in `rova/ordering/router.py`, out of this session's scope — another
executor is rebuilding the order/billing loop there in parallel. This file
proves the module itself is correct by calling `check_and_store`/`store`
directly with real, differing `dict` bodies against the real
`idempotency_key` table — the same table, same SQL, same hash function the
HTTP layer will use once a body-bearing endpoint calls it."""
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.idempotency import check_and_store, request_hash, store


def test_missing_key_is_rejected():
    class _FakeSession:
        def execute(self, *a, **k):
            raise AssertionError("should not query the DB before validating the key")

    try:
        check_and_store(_FakeSession(), key=None, principal_id="p1", method="POST", path="/x", body={})
    except ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ApiError")


def test_first_call_returns_none_then_store_persists(session):
    key = "idem-real-body-1"
    principal_id = "principal-1"
    body = {"lines": [{"index_product_id": "idx_a", "qty": 2}]}

    assert check_and_store(session, key=key, principal_id=principal_id, method="POST",
                            path="/v1/widgets", body=body) is None

    store(session, key=key, principal_id=principal_id, method="POST", path="/v1/widgets",
          body=body, status_code=200, response_body={"id": "w1"})
    session.commit()

    row = session.execute(
        text("SELECT request_hash, status_code, response_body FROM idempotency_key WHERE key=:k AND principal_id=:p"),
        {"k": key, "p": principal_id},
    ).mappings().one()
    assert row["request_hash"] == request_hash("POST", "/v1/widgets", body)
    assert row["status_code"] == 200
    assert row["response_body"] == {"id": "w1"}


def test_same_key_same_body_replays_with_replayed_flag_and_sets_header(session):
    key = "idem-real-body-2"
    principal_id = "principal-2"
    body = {"lines": [{"index_product_id": "idx_a", "qty": 3}]}
    path = "/v1/widgets"

    assert check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path, body=body) is None
    store(session, key=key, principal_id=principal_id, method="POST", path=path, body=body,
          status_code=200, response_body={"id": "w2"})
    session.commit()

    class _FakeResponse:
        def __init__(self):
            self.headers: dict[str, str] = {}

    resp = _FakeResponse()
    # a genuinely IDENTICAL body (freshly re-constructed, not the same dict
    # object) reused with the same key must replay, not conflict.
    same_body = {"lines": [{"index_product_id": "idx_a", "qty": 3}]}
    replay = check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path,
                              body=same_body, response=resp)
    assert replay is not None
    assert replay["replayed"] is True
    assert replay["status_code"] == 200
    assert replay["body"] == {"id": "w2"}
    assert resp.headers["Idempotency-Replayed"] == "true"  # was never emitted anywhere before this fix


def test_same_key_genuinely_different_body_conflicts(session):
    """The literal scenario the review proved broken (1c): the SAME
    Idempotency-Key, the SAME method+path, but a REAL difference in body
    content — must be a 409 IDEMPOTENCY_CONFLICT, never a replay of the
    first call's response."""
    key = "idem-real-body-3"
    principal_id = "principal-3"
    path = "/v1/widgets"
    first_body = {"lines": [{"index_product_id": "idx_a", "qty": 1}]}
    different_body = {"lines": [{"index_product_id": "idx_a", "qty": 999}]}  # only the qty differs

    assert check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path,
                            body=first_body) is None
    store(session, key=key, principal_id=principal_id, method="POST", path=path, body=first_body,
          status_code=200, response_body={"id": "w3", "qty": 1})
    session.commit()

    try:
        check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path, body=different_body)
    except ApiError as exc:
        assert exc.code == "IDEMPOTENCY_CONFLICT"
        assert exc.status_code == 409
    else:
        raise AssertionError("expected IDEMPOTENCY_CONFLICT for a genuinely different body on the same key")


def test_request_hash_is_canonical_regardless_of_key_order():
    """The hash must depend on the body's DATA, not incidental dict key
    insertion order or JSON whitespace — otherwise two calls that are
    semantically the same request could spuriously conflict."""
    h1 = request_hash("POST", "/v1/widgets", {"a": 1, "b": 2})
    h2 = request_hash("POST", "/v1/widgets", {"b": 2, "a": 1})
    assert h1 == h2

    h3 = request_hash("POST", "/v1/widgets", {"a": 1, "b": 3})
    assert h3 != h1  # a genuine data difference must still change the hash
