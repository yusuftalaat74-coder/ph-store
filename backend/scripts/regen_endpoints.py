"""Regenerate docs/endpoints.csv from the live OpenAPI schema."""
import csv
from pathlib import Path
from rova.main import create_app

ROOT = Path(__file__).resolve().parents[1]
schema = create_app().openapi()
rows = sorted(
    (m.upper(), p)
    for p, ms in schema["paths"].items()
    for m in ms
    if m.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
)
with (ROOT / "docs" / "endpoints.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["method", "path"])
    w.writerows(rows)
print("endpoints:", len(rows))
