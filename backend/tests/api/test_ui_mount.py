"""The web client is mounted only when a deployment asks for it.

`/app` exists so a pharmacist can open the client in a browser instead of
installing the APK. Two things must stay true: the API serves no HTML at all
unless `ROVA_UI_DIR` points at a real directory, and the mount can never
shadow an API route.
"""
from fastapi.testclient import TestClient

import rova.config
from rova.main import create_app


def _fresh_app(monkeypatch, ui_dir: str):
    """`get_settings()` memoises into a module global, so the env change only
    takes effect once that global is dropped — and it is restored afterwards
    so a later test in the same process still sees the real settings."""
    monkeypatch.setenv("ROVA_UI_DIR", ui_dir)
    monkeypatch.setattr(rova.config, "_settings", None, raising=False)
    return create_app()


def test_no_ui_is_served_by_default(monkeypatch):
    app = _fresh_app(monkeypatch, "")
    with TestClient(app) as c:
        assert c.get("/app/").status_code == 404


def test_a_directory_that_does_not_exist_does_not_break_boot(monkeypatch):
    """A deployment that points at the wrong path must still serve the API."""
    app = _fresh_app(monkeypatch, "/no/such/place")
    with TestClient(app) as c:
        assert c.get("/app/").status_code == 404
        assert c.get("/healthz").status_code == 200


def test_the_client_is_served_when_the_directory_is_real(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>PH Store</title>")
    app = _fresh_app(monkeypatch, str(tmp_path))
    with TestClient(app) as c:
        r = c.get("/app/")
        assert r.status_code == 200
        assert "PH Store" in r.text
        # and the API is untouched underneath it
        assert c.get("/healthz").status_code == 200


def test_the_mount_cannot_shadow_an_api_route(monkeypatch, tmp_path):
    """A file named like an API path must not be reachable as that path."""
    (tmp_path / "index.html").write_text("x")
    (tmp_path / "healthz").write_text("not the health endpoint")
    app = _fresh_app(monkeypatch, str(tmp_path))
    with TestClient(app) as c:
        assert c.get("/healthz").json()["status"] == "ok"
