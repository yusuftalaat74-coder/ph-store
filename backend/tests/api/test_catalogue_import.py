"""Loading the real registers and the two real price lists.

What these defend is not "the import ran" but the three judgements inside it
that a pharmacist would feel if they were wrong: a regulated product must not
end up carrying a vendor price, two pack sizes of one medicine must not
collapse into one offer with one of the prices silently dropped, and a loose
name match must never steal a different product's entry.
"""
import csv

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from rova.catalogue.importer import (REVIEWER_VENDOR, match_key, run_import,
                                     stable_id)


def _csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture()
def source(tmp_path):
    _csv(tmp_path / "index_medicines.csv",
         ["case_number", "enterprise", "commercial_name", "active_substance",
          "dosage", "form", "presentation", "manufacturer",
          "registration_number", "authorised_on"],
         [["OO/H/1", "ACME LDA", "PANADO", "Paracetamol", "500mg",
           "Comprimido", "Caixa de 20", "Adcock", "11", "01-01-2020"],
          ["OO/H/2", "ACME LDA", "CLAVAMOX 125", "Amoxicilina + Ácido Clavulânico",
           "125mg/5ml", "Pó para Suspensão Oral", "Frasco 60ml", "Lab", "54",
           "30-12-2005"]])
    _csv(tmp_path / "offers_medimport.csv",
         ["family", "designation", "dci", "price_pharmacy", "price_public",
          "min_validity"],
         [["Adcock", "PANADO 500mg CXS 10 x 12 Comp.", "Paracetamol 500 mg",
           "838.00", "1243.00", "31.10.2027"],
          ["Adcock", "PANADO 500mg CXS 20 Comp.", "Paracetamol 500 mg",
           "420.00", "620.00", "31.10.2027"]])
    _csv(tmp_path / "offers_medis.csv",
         ["laboratory", "active_substance", "commercial_name", "vat_rate",
          "price_pharmacy_ex_vat", "price_public_inc_vat"],
         [["Adcock", "Paracetamol", "PANADO 500mg CXS 10 x 12 Comp.", "0%",
           "812.00", "1200.00"]])
    return tmp_path


@pytest.fixture()
def session(db_engine):
    s = sessionmaker(bind=db_engine, future=True)()
    try:
        yield s
    finally:
        s.close()


def test_the_brand_is_found_behind_the_pack_wording():
    """`PANADO 500mg CXS 10 x 12 Comp.` is the register's `PANADO`."""
    assert match_key("PANADO 500mg CXS 10 x 12 Comp.") == match_key("Panado 500 mg")
    assert match_key("Acarbose Bluepharma 100 mg cx 50 comp") == "acarbose bluepharma 100 mg"


def test_nothing_is_loaded_as_price_regulated(session, source):
    """R-009 makes a vendor price on a regulated product unrepresentable. The
    official Diploma Ministerial figures are not in these files, so claiming
    regulation here would make every offer impossible to insert — and
    claiming it falsely is worse than admitting we do not know."""
    run_import(session, str(source))
    n = session.execute(text(
        "SELECT count(*) FROM index_product WHERE regulated_price "
        "AND reviewer_ref LIKE 'MZ-REGISTER:%'")).scalar()
    assert n == 0
    # scoped to the two vendors this importer creates: the database is shared
    # with other modules, which sell things too
    priced = session.execute(text(
        "SELECT count(*) FROM vendor_offer WHERE price IS NOT NULL "
        "AND vendor_id IN ('ven_medimport','ven_medis')")).scalar()
    assert priced == 3


def test_two_pack_sizes_keep_two_prices(session, source):
    """`uq_vendor_offer_live` allows one live offer per vendor per product.
    Both Medimport lines reduce to the same brand, so the second must get its
    own index entry instead of overwriting the first one's price."""
    run_import(session, str(source))
    rows = session.execute(text(
        "SELECT price FROM vendor_offer WHERE vendor_id = 'ven_medimport' "
        "ORDER BY price")).scalars().all()
    assert [str(p) for p in rows] == ["420.00", "838.00"]


def test_both_vendors_are_created_and_active(session, source):
    run_import(session, str(source))
    rows = session.execute(text(
        "SELECT id, status FROM vendor_account WHERE id IN "
        "('ven_medimport','ven_medis') ORDER BY id")).mappings().all()
    assert [(r["id"], r["status"]) for r in rows] == [
        ("ven_medimport", "ACTIVE"), ("ven_medis", "ACTIVE")]


def test_a_second_run_updates_prices_instead_of_duplicating(session, source):
    run_import(session, str(source))
    before = session.execute(text("SELECT count(*) FROM vendor_offer")).scalar()

    medis = source / "offers_medis.csv"
    text_in = medis.read_text(encoding="utf-8").replace("812.00", "799.00")
    medis.write_text(text_in, encoding="utf-8")

    run_import(session, str(source))
    after = session.execute(text("SELECT count(*) FROM vendor_offer")).scalar()
    assert after == before

    price = session.execute(text(
        "SELECT price FROM vendor_offer WHERE vendor_id='ven_medis'")).scalar()
    assert str(price) == "799.00"


def test_an_entry_a_human_must_check_is_marked_as_such(session, source):
    """A line the register did not clearly contain gets its own product, and
    it says so, so the review queue is one query rather than an audit."""
    run_import(session, str(source))
    refs = session.execute(text(
        "SELECT count(*) FROM index_product WHERE reviewer_ref LIKE :p"),
        {"p": f"{REVIEWER_VENDOR}:%"}).scalar()
    assert refs == 1  # the second Medimport pack size

    # and the register entries are not mislabelled as needing review
    assert session.execute(text(
        "SELECT count(*) FROM index_product WHERE reviewer_ref LIKE 'MZ-REGISTER:%'"
    )).scalar() == 2


def test_the_substance_is_searchable_across_both_vendors(session, source):
    """The comparison a pharmacist actually makes is by active substance, not
    by brand: searching `paracetamol` has to reach every vendor's SKU."""
    run_import(session, str(source))
    rows = session.execute(text(
        "SELECT DISTINCT o.vendor_id FROM vendor_offer o "
        "JOIN index_product p ON p.id = o.index_product_id "
        "WHERE p.search_text LIKE '%paracetamol%'")).scalars().all()
    assert sorted(rows) == ["ven_medimport", "ven_medis"]


def test_ids_are_stable_so_a_reimport_is_the_same_catalogue():
    a = stable_id("idx_", "MED", "OO/H/1", "PANADO", "500mg")
    b = stable_id("idx_", "MED", "OO/H/1", "PANADO", "500mg")
    assert a == b and a.startswith("idx_") and len(a) == 24


# ── categories ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("form,name,expected", [
    # the defect this table exists for: `gel` is inside `Angelic`, and the
    # first version of the categoriser filed a box of 28 tablets under
    # topicals because of it. Found on the live storefront.
    ("", "Angelic Cx. 28 Comp.", "Comprimidos"),
    ("", "Magnesium B Cxs X 30 Comp.", "Comprimidos"),
    ("", "Acarbose Bluepharma 100 mg cx 50 comp", "Comprimidos"),
    ("Comprimidos", "PANADO 500", "Comprimidos"),
    ("", "Aciclovir Bluepharma 50 mg/g Créme, Bisnaga de 10 g", "Tópicos"),
    ("", "AquaMaris, Spray Nasal, Frasco de 30 ml", "Tópicos"),
    ("", "ACARILBIAL Sol. Cutânea 200 ml", "Tópicos"),
    ("Xarope", "BENYLIN", "Orais líquidos"),
    ("Solução Injectável", "CEFTRIAXONA", "Injectáveis"),
    ("Comprimidos vaginais", "CLOTRIMAZOL", "Vaginais e supositórios"),
    ("Colírio", "TOBRADEX", "Oftálmicos e óticos"),
    ("Cápsulas", "OMEPRAZOL", "Cápsulas"),
    # a powder that becomes a syrup is bought as a syrup — the liquid rule
    # is ahead of the powder rule on purpose, and this pins that order
    ("Pó para Suspensão Oral", "CLAVAMOX", "Orais líquidos"),
    ("Granulado", "FOSFOMICINA", "Pós e granulados"),
    ("", "ACUTIL Cxs X 20 Saquetas", "Pós e granulados"),
])
def test_a_product_is_filed_by_whole_words_not_fragments(form, name, expected):
    from rova.catalogue.importer import categorise
    assert categorise("", form, name) == expected


def test_a_register_category_wins_over_a_guess():
    """`Dispositivo Médico de Diagnóstico In-vitro` is what the register
    says; no keyword rule should overrule it."""
    from rova.catalogue.importer import categorise
    stated = "Dispositivo Médico de Diagnóstico In-vitro"
    assert categorise(stated, stated, "β-HCG Rapid Test Kit Spray") == stated


def test_nothing_falls_out_of_every_filter():
    """A product with nothing to go on is filed under `Outros`, never left
    without a category — an uncategorised row is invisible to the whole
    category filter and nobody would notice it was missing."""
    from rova.catalogue.importer import categorise
    assert categorise("", "", "") == "Outros"
    assert categorise("", "", "XYZQ 17") == "Outros"


def test_every_rule_in_the_table_is_reachable():
    """Each label has at least one case above proving it fires, so a rule
    cannot be shadowed into uselessness by one written earlier."""
    from rova.catalogue.importer import CATEGORY_RULES, categorise
    samples = {
        "Vaginais e supositórios": ("Comprimidos vaginais", "X"),
        "Oftálmicos e óticos": ("Colírio", "X"),
        "Injectáveis": ("Solução Injectável", "X"),
        "Tópicos": ("Creme", "X"),
        "Orais líquidos": ("Xarope", "X"),
        "Comprimidos": ("Comprimidos", "X"),
        "Cápsulas": ("Cápsulas", "X"),
        "Pós e granulados": ("Granulado", "X"),
    }
    for label, _ in CATEGORY_RULES:
        form, name = samples[label]
        assert categorise("", form, name) == label, f"{label} is unreachable"
