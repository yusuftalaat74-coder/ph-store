"""Turn the five Mozambican source PDFs into clean CSV.

These are ruled tables, so pdfplumber's line-based extraction gets the cells
right where a text-position guess would not: several columns overflow into
their neighbour in `pdftotext -layout` output (`OO/H/200310/21MEDIMPORT LDA`),
and a value that wraps onto a second visual line arrives as its own row with
an empty first cell. Both are handled here rather than being left for the
importer to guess at.
"""
import csv
import pathlib
import re
import sys

import pdfplumber

SRC = pathlib.Path("/root/.claude/uploads/75ebd008-0946-5787-9bf9-7e8d628c7f21")
OUT = pathlib.Path("/home/claude/data")
OUT.mkdir(exist_ok=True)


def rows(pdf_name, *, n_cols, first_col_starts_row=True):
    """Every data row of a ruled table, continuation lines folded back in.

    `first_col_starts_row`: a real row begins with a non-empty first cell
    (the item number). A row whose first cell is blank is the tail of the
    previous one and its cells are appended to it.
    """
    out = []
    with pdfplumber.open(SRC / pdf_name) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for raw in table:
                cells = [(c or "").replace("\n", " ").strip() for c in raw[:n_cols]]
                cells += [""] * (n_cols - len(cells))
                if not any(cells):
                    continue
                head = cells[0]
                if first_col_starts_row and not head and out:
                    for i, c in enumerate(cells):
                        if c:
                            out[-1][i] = (out[-1][i] + " " + c).strip()
                    continue
                out.append(cells)
    return out


def is_header(cells):
    joined = " ".join(cells).upper()
    return any(k in joined for k in ("ITEM", "CASE NUMBER", "COMMERCIAL NAME",
                                      "PRODUCT TYPE", "ENTERPRISE  ", "LABORATORIO",
                                      "NOME COMERCIAL", "DESIGNAÇÃO"))


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def money(s):
    """`1 374,00` and `704,00` -> `1374.00`. Anything else -> ''."""
    s = (s or "").replace(" ", " ").strip()
    s = re.sub(r"[^\d,.\s]", "", s).replace(" ", "")
    if not s:
        return ""
    # Portuguese decimal comma, optional thousands dot
    s = s.replace(".", "") if "," in s else s
    s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return ""
    return f"{v:.2f}" if v > 0 else ""


def write(name, header, records):
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(records)
    print(f"{name:34} {len(records):>5} rows")
    return len(records)


# ── 1. the national medicines register ────────────────────────────────────
med = []
for c in rows("fe5a7470-BD_Medicamentos.pdf", n_cols=14):
    if is_header(c) or not re.fullmatch(r"\d+", c[0]):
        continue
    name, substance = clean(c[3]), clean(c[4])
    if not name:
        continue
    med.append([clean(c[1]), clean(c[2]), name, substance, clean(c[5]),
                clean(c[6]), clean(c[7]), clean(c[8]), clean(c[10]), clean(c[11])])
write("index_medicines.csv",
      ["case_number", "enterprise", "commercial_name", "active_substance",
       "dosage", "form", "presentation", "manufacturer",
       "registration_number", "authorised_on"], med)

# ── 2. authorised non-medicine products (devices, antiseptics, …) ─────────
aut = []
for c in rows("a892d4eb-BD_Lista_de_produtos_autorizados.csv.pdf", n_cols=7):
    if is_header(c) or not re.fullmatch(r"\d+", c[0]):
        continue
    name = clean(c[3])
    if not name:
        continue
    aut.append([clean(c[1]), clean(c[2]), name, clean(c[4]), clean(c[5]), clean(c[6])])
write("index_authorised.csv",
      ["category", "enterprise", "product", "manufacturer",
       "authorised_on", "registration_number"], aut)

# ── 3. phytotherapeutics and supplements ─────────────────────────────────
fit = []
for c in rows("9ad6d3e9-Fitotera_picos.pdf", n_cols=6, first_col_starts_row=False):
    if is_header(c):
        continue
    kind, enterprise, name = clean(c[0]), clean(c[1]), clean(c[2])
    if not name or not kind:
        continue
    fit.append([kind, enterprise, name, clean(c[3]), clean(c[4]), clean(c[5])])
write("index_phyto.csv",
      ["kind", "enterprise", "product", "manufacturer", "col5", "col6"], fit)

# ── 4. Medimport price list ──────────────────────────────────────────────
mi = []
for c in rows("7da1c475-Lista_Prec_os_Medimport.pdf", n_cols=6, first_col_starts_row=False):
    if is_header(c):
        continue
    family, designation, dci = clean(c[0]), clean(c[1]), clean(c[2])
    pvf, pvp, validity = money(c[3]), money(c[4]), clean(c[5])
    if not designation or not pvf:
        continue
    mi.append([family, designation, dci, pvf, pvp, validity])
write("offers_medimport.csv",
      ["family", "designation", "dci", "price_pharmacy", "price_public",
       "min_validity"], mi)

# ── 5. Medis price list ──────────────────────────────────────────────────
ms = []
for c in rows("4db3f392-Lista_Medis_22-01-2026.pdf", n_cols=6, first_col_starts_row=False):
    if is_header(c):
        continue
    lab, substance, name = clean(c[0]), clean(c[1]), clean(c[2])
    vat, pharmacy, public = clean(c[3]), money(c[4]), money(c[5])
    if not name or not pharmacy:
        continue
    ms.append([lab, substance, name, vat, pharmacy, public])
write("offers_medis.csv",
      ["laboratory", "active_substance", "commercial_name", "vat_rate",
       "price_pharmacy_ex_vat", "price_public_inc_vat"], ms)
