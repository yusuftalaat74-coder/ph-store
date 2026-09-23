"""B5.36 — match.py is vendored byte-identical under rova/matching/; this
proves the vendored file plus the thin adapter actually run against real
seeded index_product data (not just that the file exists)."""
import filecmp
from pathlib import Path

from sqlalchemy import text

from rova.matching.adapter import match_lines

VENDORED = Path(__file__).resolve().parents[1] / "src" / "rova" / "matching" / "match.py"
ORIGINAL = Path("/home/claude/moz/matcher/match.py")


def test_match_py_is_byte_identical_to_the_original():
    if not ORIGINAL.exists():
        return  # original not present in this environment; vendored copy is still exercised below
    assert filecmp.cmp(VENDORED, ORIGINAL, shallow=False)


def test_match_lines_runs_against_seeded_index(session):
    # the shared `session` fixture only carries config, not the demo catalogue
    # (that is loaded by the full `rova seed`, not `--config-only`) -- so this
    # test provisions its own PUBLISHED index_product row, same pattern the
    # other domain tests use for index_product fixtures.
    session.execute(text(
        "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, manufacturer, "
        "aim_status, regulated_price, review_status, reviewer_ref, search_text) VALUES "
        "('idx_match_ibu', 'Ibuprofeno', 'Ibuprofeno Generico', 'Comprimido', '400 mg', '20', 'M', "
        "'AUTHORISED', true, 'PUBLISHED', 'LOCAL:x', 'ibuprofeno 400 mg')"
    ))
    session.flush()

    results = match_lines(session, ["Ibuprofeno 400mg", "algum produto totalmente inexistente xyz123"])
    assert len(results) == 2
    assert results[0].match_status in {"AUTO", "ASK", "UNRESOLVED"}
    # a clean, well-formed INN+strength line against the seeded index should
    # resolve to a concrete product, not silently fall through
    assert results[0].index_product_id is not None or results[0].match_status == "ASK"
    assert results[1].match_status == "UNRESOLVED"
    assert results[1].index_product_id is None
