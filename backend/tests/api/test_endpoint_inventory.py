"""A17 'Endpoint inventory' row / item 5 (backend-review-r1.md): docs/
endpoints.csv is the checked-in manifest of every route this backend
registers; this test regenerates the same (method, path) set live from the
running app's own OpenAPI schema and asserts they're identical. A route
added or removed without updating docs/endpoints.csv fails here — the
manifest is not free-standing documentation, it's load-bearing."""
import csv
from pathlib import Path

from rova.main import create_app

DOCS_CSV = Path(__file__).resolve().parents[2] / "docs" / "endpoints.csv"
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _live_endpoints() -> set[tuple[str, str]]:
    schema = create_app().openapi()
    return {
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        for method in methods
        if method.upper() in _HTTP_METHODS
    }


def _csv_endpoints() -> set[tuple[str, str]]:
    with DOCS_CSV.open(newline="") as f:
        return {(row["method"], row["path"]) for row in csv.DictReader(f)}


def test_endpoints_csv_exists_and_is_non_empty():
    assert DOCS_CSV.exists(), "docs/endpoints.csv is missing"
    assert _csv_endpoints(), "docs/endpoints.csv has no rows"


def test_endpoints_csv_matches_live_openapi_exactly():
    live = _live_endpoints()
    documented = _csv_endpoints()
    missing_from_csv = live - documented
    stale_in_csv = documented - live
    assert not missing_from_csv, f"routes registered but not in docs/endpoints.csv: {sorted(missing_from_csv)}"
    assert not stale_in_csv, f"docs/endpoints.csv lists routes no longer registered: {sorted(stale_in_csv)}"
