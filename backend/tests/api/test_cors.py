"""CORS, specifically for the Android client.

The app's page is loaded from `file:///android_asset/index.html`, so its
`Origin` header is the literal string `null`. If the API does not answer the
preflight for that origin, every call from the phone fails before it is even
sent, and the app silently looks "broken" with nothing in the server log.
"""


def test_preflight_from_the_android_null_origin_is_allowed(client):
    r = client.options(
        "/v1/auth/login",
        headers={"Origin": "null", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type,authorization"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "*"
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed or allowed == "*"


def test_credentials_are_not_allowed(client):
    """This API authenticates with a Bearer header, never a cookie. Turning
    credentials on while origins are `*` is refused by the CORS spec, and
    would be the wrong fix for any problem it appeared to solve."""
    r = client.options(
        "/v1/auth/login",
        headers={"Origin": "null", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-credentials") is None


def test_an_actual_request_carries_the_allow_origin_header(client, db_engine):
    r = client.post("/v1/auth/login",
                    headers={"Origin": "null"},
                    json={"phone": "+000000000000", "password": "nope", "surface": "PH"})
    # the credentials are wrong on purpose — what matters is the CORS header
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.status_code == 401
