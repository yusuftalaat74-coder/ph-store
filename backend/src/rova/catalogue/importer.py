"""Load the Mozambican registers and two real vendor price lists.

The seed's twelve invented products were always a placeholder. This module
replaces them with the actual national register and with what Medimport and
Medis really sell, so search, ranking and vendor comparison run on real data.

Five files, produced by `scripts/extract_catalogue.py` from the published
PDFs, are read from one directory:

    index_medicines.csv     the national medicines register
    index_authorised.csv    authorised devices, antiseptics and the rest
    index_phyto.csv         phytotherapeutics and supplements
    offers_medimport.csv    Medimport's price list
    offers_medis.csv        Medis' price list

Three things this importer refuses to invent, because inventing them would
put a number in front of a pharmacist that nobody can stand behind:

* **Regulated prices.** `Diploma Ministerial 21/2017` is not in these files,
  so every product is loaded with `regulated_price = false`. That is not a
  claim that none is regulated — it is the only state that is honest about
  what we know, and it keeps R-009 satisfied (a regulated product may carry
  no vendor price at all). Loading the official list later flips the flag,
  and the composite FK will refuse the flip while a priced offer exists,
  which is exactly the review that should happen.
* **Stock.** A price list says what a vendor sells, never how much is on the
  shelf. Offers are loaded with `qty_available = assume_stock` and the
  vendor's own `acceptance_mode` decides: under `MANUAL_CONFIRM` the vendor
  confirms quantity when accepting the order, which is how these two work.
* **A match it is not sure of.** An offer whose product is not clearly in the
  register gets its own index entry carrying `reviewer_ref = VENDOR-LIST:…`,
  so the entries a human still has to check are one query away instead of
  being silently merged into a register row they may not be.

The import is re-runnable: every insert is keyed on a deterministic id, so a
second run updates prices rather than duplicating the catalogue.
"""
from __future__ import annotations

import csv
import hashlib
import pathlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

REVIEWER_REGISTER = "MZ-REGISTER"
REVIEWER_VENDOR = "VENDOR-LIST"

VENDORS = {
    "medimport": {
        "id": "ven_medimport",
        "org_id": "org_medimport",
        "tax_id": "MZ-MEDIMPORT",
        "legal_name": "MEDIMPORT, LDA",
        "trade_name": "Medimport",
        "file": "offers_medimport.csv",
    },
    "medis": {
        "id": "ven_medis",
        "org_id": "org_medis",
        "tax_id": "MZ-MEDIS",
        "legal_name": "MEDIS, LDA",
        "trade_name": "Medis",
        "file": "offers_medis.csv",
    },
}


# ── normalisation ─────────────────────────────────────────────────────────

_PACK_NOISE = re.compile(
    r"\b(cx|cxs|caixa|caixas|comp|comprimidos?|c[aá]ps(ulas?)?|frs?|frasco|"
    r"bisnaga|saquetas?|amp|ampolas?|susp|sol|xarope|eferv|x)\b", re.I)


def fold(s: str) -> str:
    """Lower-case, accent-stripped, punctuation-free — what search and
    matching both compare on. `Ácido Clavulânico` and `ACIDO CLAVULANICO`
    are the same string here, which they must be: the register shouts in
    capitals and the price lists do not."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def match_key(name: str) -> str:
    """A product name with pack chatter removed, for comparing a register
    entry against a price-list line. `PANADO 500mg CXS 10 x 12 Comp.` and
    `PANADO 500 mg` both reduce to `panado 500 mg`."""
    s = fold(name)
    s = re.sub(r"(\d)\s*(mg|ml|g|mcg|ui|iu)\b", r"\1 \2", s)
    s = _PACK_NOISE.sub(" ", s)
    s = re.sub(r"\b\d{1,3}\s*(un|units?)?\b(?!\s*(mg|ml|g|mcg|ui|iu))", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _lookup_keys(name: str, substance: str):
    """Keys to try against the register, most specific first.

    A register entry is `PANADO` with its dosage in a separate column; a
    price list writes `PANADO 500mg CXS 10 x 12 Comp.`. Matching the whole
    line finds almost nothing, so the leading one or two words — the brand,
    before any strength or pack wording — are tried as well. Order matters:
    a one-word prefix is the loosest key here and must never be reached
    before an exact one, or `PANADO PAED. SYRUP` would take the tablet's
    entry away from it.
    """
    full = match_key(name)
    words = full.split()
    yield full
    if substance:
        yield match_key(substance)
    if len(words) >= 2:
        yield " ".join(words[:2])
    if words:
        yield words[0]


def stable_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha1("\u0001".join(p or "" for p in parts).encode()).hexdigest()[:20]
    return f"{prefix}{h}"


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ── what we are about to write ────────────────────────────────────────────

@dataclass
class Product:
    id: str
    inn: str
    brand_name: str
    form: str
    strength: str
    pack_size: str
    manufacturer: str
    therapeutic_class: str | None
    reviewer_ref: str
    search_text: str


@dataclass
class Report:
    products_register: int = 0
    products_from_vendor: int = 0
    offers: dict[str, int] = field(default_factory=dict)
    matched: int = 0
    unmatched: int = 0

    def lines(self) -> list[str]:
        out = [
            f"index products from the registers   {self.products_register:>6}",
            f"index products created from a list  {self.products_from_vendor:>6}"
            f"   (reviewer_ref {REVIEWER_VENDOR}:*, need a human)",
            f"offers matched to a register entry  {self.matched:>6}",
            f"offers on a vendor-created entry    {self.unmatched:>6}",
        ]
        for name, n in self.offers.items():
            out.append(f"offers loaded — {name:<18} {n:>6}")
        return out


# ── building the index ────────────────────────────────────────────────────

def _product_from_medicine(r: dict) -> Product | None:
    brand = (r.get("commercial_name") or "").strip()
    if not brand:
        return None
    inn = (r.get("active_substance") or "").strip() or brand
    strength = (r.get("dosage") or "").strip()
    form = (r.get("form") or "").strip() or "Não especificado"
    pack = (r.get("presentation") or "").strip() or "1"
    manufacturer = (r.get("manufacturer") or "").strip() or (r.get("enterprise") or "").strip() or "—"
    ref = (r.get("case_number") or r.get("registration_number") or brand).strip()
    return Product(
        id=stable_id("idx_", "MED", ref, brand, strength),
        inn=inn, brand_name=brand, form=form, strength=strength, pack_size=pack,
        manufacturer=manufacturer, therapeutic_class=None,
        reviewer_ref=f"{REVIEWER_REGISTER}:{ref}",
        search_text=fold(" ".join([brand, inn, strength, form])),
    )


def _product_from_authorised(r: dict) -> Product | None:
    name = (r.get("product") or "").strip()
    if not name:
        return None
    category = (r.get("category") or "").strip()
    ref = (r.get("registration_number") or name).strip()
    return Product(
        id=stable_id("idx_", "AUT", ref, name),
        inn=name, brand_name=name, form=category or "Produto autorizado",
        strength="", pack_size="1",
        manufacturer=(r.get("manufacturer") or r.get("enterprise") or "—").strip() or "—",
        therapeutic_class=category or None,
        reviewer_ref=f"{REVIEWER_REGISTER}:{ref}",
        search_text=fold(" ".join([name, category])),
    )


def _product_from_phyto(r: dict) -> Product | None:
    name = (r.get("product") or "").strip()
    if not name:
        return None
    kind = (r.get("kind") or "Fitoterápico").strip()
    enterprise = (r.get("enterprise") or "").strip()
    return Product(
        id=stable_id("idx_", "FIT", enterprise, name),
        inn=name, brand_name=name, form=kind, strength="", pack_size="1",
        manufacturer=(r.get("manufacturer") or enterprise or "—").strip() or "—",
        therapeutic_class=kind,
        reviewer_ref=f"{REVIEWER_REGISTER}:{kind}:{name}"[:200],
        search_text=fold(" ".join([name, kind, enterprise])),
    )


def _product_from_offer(vendor_key: str, name: str, substance: str,
                        maker: str) -> Product:
    return Product(
        id=stable_id("idx_", "VND", vendor_key, name),
        inn=substance or name, brand_name=name,
        form="Não especificado", strength="", pack_size="1",
        manufacturer=maker or "—", therapeutic_class=None,
        reviewer_ref=f"{REVIEWER_VENDOR}:{vendor_key}",
        search_text=fold(" ".join([name, substance, maker])),
    )


# ── writing ───────────────────────────────────────────────────────────────

_UPSERT_PRODUCT = text("""
INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size,
                           manufacturer, therapeutic_class, aim_status,
                           regulated_price, review_status, reviewer_ref, search_text)
VALUES (:id, :inn, :brand, :form, :strength, :pack, :maker, :klass,
        'AUTHORISED', false, 'PUBLISHED', :ref, :search)
ON CONFLICT (id) DO UPDATE SET
    inn = EXCLUDED.inn, brand_name = EXCLUDED.brand_name, form = EXCLUDED.form,
    strength = EXCLUDED.strength, pack_size = EXCLUDED.pack_size,
    manufacturer = EXCLUDED.manufacturer,
    therapeutic_class = EXCLUDED.therapeutic_class,
    search_text = EXCLUDED.search_text, updated_at = now()
""")

_UPSERT_OFFER = text("""
INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price,
                          qty_available, pack_size, expiry_horizon_days, price,
                          stock_confirmed_at, freshness_state)
VALUES (:id, :vendor, :product, false, :qty, :pack, :horizon, :price,
        :confirmed, 'FRESH')
ON CONFLICT (id) DO UPDATE SET
    price = EXCLUDED.price, qty_available = EXCLUDED.qty_available,
    expiry_horizon_days = EXCLUDED.expiry_horizon_days,
    stock_confirmed_at = EXCLUDED.stock_confirmed_at,
    freshness_state = 'FRESH', withdrawn_at = NULL, updated_at = now()
""")


def _write_product(session: Session, p: Product) -> None:
    session.execute(_UPSERT_PRODUCT, {
        "id": p.id, "inn": p.inn[:400], "brand": p.brand_name[:400],
        "form": p.form[:200], "strength": p.strength[:200],
        "pack": p.pack_size[:200], "maker": p.manufacturer[:300],
        "klass": (p.therapeutic_class or None), "ref": p.reviewer_ref[:200],
        "search": p.search_text[:1000],
    })


def _ensure_vendor(session: Session, v: dict, region: str) -> None:
    session.execute(text("""
        INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type)
        VALUES (:id, :tax, :name, :norm, 'VENDOR') ON CONFLICT (id) DO NOTHING
    """), {"id": v["org_id"], "tax": v["tax_id"], "name": v["legal_name"],
           "norm": fold(v["legal_name"])})
    session.execute(text("""
        INSERT INTO vendor_account (id, organisation_id, region_code, trade_name,
                                    vendor_type, delivery_mode, mov_amount,
                                    acceptance_mode, status)
        VALUES (:id, :org, :region, :trade, 'IMPORTER_WHOLESALER',
                'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')
        ON CONFLICT (id) DO UPDATE SET status = 'ACTIVE', trade_name = EXCLUDED.trade_name
    """), {"id": v["id"], "org": v["org_id"], "region": region,
           "trade": v["trade_name"]})


def _horizon_days(validity: str) -> int:
    """`31.10.2027` -> days from today, floored at 0. Missing or unparseable
    dates fall back to 180, which is the shortest horizon the ranking engine
    treats as ordinary rather than near-expiry."""
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime((validity or "").strip(), fmt).date()
        except ValueError:
            continue
        return max((d - date.today()).days, 0)
    return 180


def run_import(session: Session, directory: str, *, assume_stock: int = 500,
               region: str = "MAPUTO_CIDADE") -> Report:
    base = pathlib.Path(directory)
    rep = Report()

    # 1. the registers
    by_key: dict[str, str] = {}   # match_key -> index_product.id
    for filename, build in (("index_medicines.csv", _product_from_medicine),
                            ("index_authorised.csv", _product_from_authorised),
                            ("index_phyto.csv", _product_from_phyto)):
        for row in _rows(base / filename):
            p = build(row)
            if p is None:
                continue
            _write_product(session, p)
            rep.products_register += 1
            by_key.setdefault(match_key(p.brand_name), p.id)
            if p.inn and p.inn != p.brand_name:
                by_key.setdefault(match_key(f"{p.inn} {p.strength}"), p.id)
    session.flush()

    # 2. the vendors and their offers
    now = datetime.now(timezone.utc)
    for key, v in VENDORS.items():
        _ensure_vendor(session, v, region)
        loaded = 0
        # `uq_vendor_offer_live` allows one live offer per (vendor, product).
        # Two pack sizes of the same medicine are two SKUs on the price list
        # but reduce to one `match_key`, so the second would collide. It gets
        # its own index entry, built from the line's full designation, which
        # keeps the pack distinction the pharmacist is actually buying rather
        # than silently dropping one of the two prices.
        used: set[str] = set()
        seen_offers: set[str] = set()
        for row in _rows(base / v["file"]):
            name = (row.get("designation") or row.get("commercial_name") or "").strip()
            price = (row.get("price_pharmacy") or row.get("price_pharmacy_ex_vat") or "").strip()
            if not name or not price:
                continue
            offer_id = stable_id("ofr_", key, name)
            if offer_id in seen_offers:      # the same line twice in one file
                continue
            seen_offers.add(offer_id)

            substance = (row.get("dci") or row.get("active_substance") or "").strip()
            maker = (row.get("family") or row.get("laboratory") or "").strip()

            product_id = None
            for candidate in _lookup_keys(name, substance):
                product_id = by_key.get(candidate)
                if product_id is not None:
                    break
            matched = product_id is not None and product_id not in used
            if not matched:
                p = _product_from_offer(key, name, substance, maker)
                _write_product(session, p)
                by_key.setdefault(match_key(p.brand_name), p.id)
                product_id = p.id
                rep.products_from_vendor += 1
                rep.unmatched += 1
            else:
                rep.matched += 1
            used.add(product_id)

            session.execute(_UPSERT_OFFER, {
                "id": offer_id,
                "vendor": v["id"], "product": product_id,
                "qty": assume_stock, "pack": "1",
                "horizon": _horizon_days(row.get("min_validity", "")),
                "price": price, "confirmed": now,
            })
            loaded += 1
        rep.offers[v["trade_name"]] = loaded
        session.flush()

    session.commit()
    return rep
