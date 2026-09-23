#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
match.py -- Drug name matcher for a Mozambican pharmacy stock list against a
drug Index.

    python3 match.py --items pharmacy_items.csv --index index.csv \
                      --aliases aliases.json --out report.xlsx

GOVERNING RULE (do not weaken this without re-reading the brief):
    A wrong match is far more dangerous than no match. The matcher only ever
    outputs one of three states per row:

        AUTO       high confidence   -> matched automatically
        ASK        medium confidence -> top 3 candidates surfaced for a human
        UNMATCHED  low confidence    -> left open, never guessed

    A name-only match (brand/INN text similarity, with no barcode and no
    strong alias-frequency signal) NEVER reaches AUTO by itself when the
    pharmacy's raw text is missing strength/pack, or when the strength/form
    it does carry conflicts with the candidate. The two signals allowed to
    push a row to AUTO without structural corroboration are (1) an exact
    barcode match and (2) an alias the dictionary has already seen enough
    times to trust (ALIAS_AUTO_MIN_COUNT). Everything else needs the name
    AND the structured fields (strength/form/pack) to agree.

The matching ladder (all seven rungs are implemented):
    1. exact              -- normalised raw text equals an index brand/INN
    2. normalisation       -- diacritics/tashkeel stripped, ph/f, c/k, y/i
                              unified, Arabic-Indic digits -> Latin, pack
                              noise ("20 comp", "cx 21", "علبة 20") stripped
    3. alias dictionary    -- every alias learned from every pharmacy so far
    4. fuzzy                -- edit distance / token-set ratio on the
                              drug-name core, AFTER strength/form/pack are
                              stripped out of both sides
    5. structured           -- INN + strength + form + pack size agreement
    6. semantic             -- character n-gram / TF-IDF cosine, for
                              invented or heavily abbreviated names (no
                              model download, no network call)
    7. human                -- unresolved rows go to a human; every human
                              answer is written back into the alias
                              dictionary (see --resolutions below)
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date

# Optional: rapidfuzz gives faster/better token-set fuzzy ratios. If it is
# not installed we fall back to the standard-library difflib, scaled to the
# same 0-100 range, so the tool always runs with only pandas/openpyxl as
# hard dependencies (per the brief: "no network calls at runtime, no model
# downloads").
try:
    from rapidfuzz import fuzz as _rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    import difflib
    HAVE_RAPIDFUZZ = False

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ===========================================================================
# THRESHOLDS -- named constants, not truths. Every one of these is a design
# assumption picked to behave sensibly on the demo data in this folder. They
# MUST be re-tuned against the owner's real pharmacy list and Index once he
# has run this a few times -- treat the first few real runs as a calibration
# exercise, not a verdict on the tool.
# ===========================================================================

# --- Barcode -----------------------------------------------------------
# A barcode match is treated as decisive on its own: two different physical
# products essentially never share a GTIN/EAN. If more than one Index row
# somehow carries the same barcode (a dirty Index), we do NOT auto-trust it
# and fall back to normal scoring across just those rows.
BARCODE_STRIP_RE = re.compile(r"[^0-9]")

# --- Alias dictionary ----------------------------------------------------
# "If 40 pharmacies mapped X to the same id, the 41st is near-certain."
# ALIAS_AUTO_MIN_COUNT is how many independent prior confirmations (human
# decisions, or auto-matches later folded back in) an alias needs before we
# trust it alone, without requiring the strength/pack to also agree. Set low
# here (3) only because the demo simulates "prior pharmacies" by seeding
# aliases.json directly -- on real data this should probably start higher
# (10-20) until the dictionary has a track record, then can be lowered.
ALIAS_AUTO_MIN_COUNT = 3

# An alias seen once or twice is worth surfacing to a human as a strong
# candidate, but not yet worth trusting blindly.
ALIAS_ASK_MIN_COUNT = 1

# --- Name similarity (0-100 scale, rapidfuzz/difflib token-set ratio) ---
# Minimum text-similarity score, TOGETHER WITH matching structure (strength
# and/or pack), to accept a name-based match automatically. This is the
# rung-4/5 combination: fuzzy name + structured agreement.
NAME_SIM_AUTO_MIN = 82

# When strength AND form AND pack all agree (full structural agreement --
# the strongest version of rung 5), that alone narrows the candidate space
# so much that we accept a much weaker name score, because three
# independent structured fields lining up by coincidence is very unlikely.
# This is what lets an abbreviation like "COZ 50mg COMP X30" auto-match
# Cozaar 50mg comprimido x30 even though the text itself barely matches.
NAME_SIM_AUTO_MIN_FULL_STRUCTURE = 50

# Minimum score to even bother a human with a candidate. Below this a
# candidate is noise, not a real option, and is dropped from the ASK list.
NAME_SIM_ASK_MIN = 60

# --- Semantic / character n-gram (rung 6), 0-100 scale ------------------
# For invented, transliterated or heavily abbreviated names where the
# token-based fuzzy ratio is weak. This threshold is deliberately lower
# than NAME_SIM_ASK_MIN because n-gram cosine similarity on short strings
# is naturally noisier -- it is a last-resort signal to avoid an outright
# UNMATCHED, not a confidence claim.
SEMANTIC_ASK_MIN = 42
NGRAM_SIZE = 3

# --- Structured fields ----------------------------------------------------
# Numeric strength tolerance: treats "500mg" vs "500.0mg" as equal but
# "500mg" vs "250mg" as a hard conflict. Kept tight on purpose -- strength
# is exactly the field where a silent wrong guess is clinically dangerous
# (Amoxil 500 vs Amoxil 250).
STRENGTH_TOLERANCE_PCT = 0.05

# Points added/subtracted for structured agreement/conflict when computing
# the combined score. Strength conflict is penalised hardest because it is
# the specific danger called out in the brief; pack conflict is penalised
# least because pack size differences are often just re-boxing.
STRENGTH_AGREE_BONUS = 18
STRENGTH_CONFLICT_PENALTY = 70
FORM_AGREE_BONUS = 8
FORM_CONFLICT_PENALTY = 30
PACK_AGREE_BONUS = 6
PACK_CONFLICT_PENALTY = 12

# --- Price (weak corroborating signal only, never decisive alone) -------
PRICE_PROXIMITY_PCT = 0.20
PRICE_AGREE_BONUS = 4
PRICE_WILD_MISMATCH_RATIO = 3.0
PRICE_MISMATCH_PENALTY = 6

# --- Candidate ranking -----------------------------------------------------
# The best candidate must beat the runner-up by at least this many points
# on the combined 0-100 score, or the two are "too close to call" and the
# row is downgraded from AUTO to ASK regardless of the raw score.
MIN_SCORE_MARGIN_FOR_AUTO = 8

# Same-INN, same-strength/form/pack, different-brand candidates (e.g.
# Amoxil vs Clamoxyl vs generic Amoxicilina, all 500mg capsules) need a much
# bigger lead before the name alone is trusted to pick the brand, because a
# short or generic name is often genuinely compatible with any of them.
MIN_SCORE_MARGIN_FOR_AUTO_SAME_INN = 20

TOP_K_CANDIDATES = 3

# ===========================================================================
# Normalisation pipeline (ladder rung 2)
# ===========================================================================

# Combining marks cover both Portuguese accents (á, ã, ç -> a, a, c once the
# base letter is separated by NFKD) and Arabic tashkeel (fatha/damma/kasra/
# shadda/sukun are all combining marks too), so one strip handles both.
def _strip_combining_marks(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))

_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_EXTENDED_ARABIC_INDIC_DIGITS = "۰۱۲۳۴۵۶۷۸۹"  # Persian/Urdu variant, seen occasionally

def _unify_digits(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ARABIC_INDIC_DIGITS:
            out.append(str(_ARABIC_INDIC_DIGITS.index(ch)))
        elif ch in _EXTENDED_ARABIC_INDIC_DIGITS:
            out.append(str(_EXTENDED_ARABIC_INDIC_DIGITS.index(ch)))
        else:
            out.append(ch)
    return "".join(out)

_ARABIC_RANGE_RE = re.compile(r"[؀-ۿ]")

def contains_arabic(s: str) -> bool:
    return bool(_ARABIC_RANGE_RE.search(s))

# Rough consonant-skeleton transliteration of Arabic script to Latin. Arabic
# has no letter for "p", so pharmacists almost always write brand names with
# "p" sounds (Panadol, Ampicilina...) using ب (b) -- we normalise b<->p only
# on strings that actually went through this transliteration path, so it
# never blurs distinctions between two genuinely different Latin-script
# products.
_AR_TO_LATIN = {
    "ا": "a", "أ": "a", "إ": "a", "آ": "a", "ب": "b", "ت": "t", "ث": "th",
    "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a",
    "غ": "gh", "ف": "f", "ق": "k", "ك": "k", "ل": "l", "م": "m", "ن": "n",
    "ه": "h", "و": "u", "ي": "i", "ى": "a", "ة": "a", "ء": "",
}

def transliterate_arabic(s: str) -> str:
    if not contains_arabic(s):
        return s
    out = []
    for ch in s:
        out.append(_AR_TO_LATIN.get(ch, ch))
    latin = "".join(out)
    # b/p are the same letter in Arabic script; only fold them together here.
    latin = latin.replace("b", "p")
    return latin

_PACK_NOISE_PATTERNS = [
    r"\bcx\.?\s*\d+\b",
    r"\bcaixa\s*(de)?\s*\d+\b",
    r"\bembalagem\s*(de)?\s*\d+\b",
    r"\bkit\s*c\/?\s*\d+\b",
    r"\bblister\s*\d+\b",
    r"\bx\s*\d+\b",
    r"\b\d+\s*(comprimidos?|comp|cpr|cps|caps?|capsulas?|tabs?|tablets?)\b",
    r"\bfrasco\s*\d*\s*m?l?\b",
    r"علبة\s*\d+",
    r"كرتونة\s*\d+",
]
_PACK_NOISE_RE = re.compile("|".join(_PACK_NOISE_PATTERNS), re.IGNORECASE)

_PACK_SIZE_CAPTURE_RE = re.compile(
    r"(?:\bcx\.?\s*|\bcaixa\s*(?:de)?\s*|\bx\s*|\bblister\s*|\bkit\s*c\/?\s*|علبة\s*|كرتونة\s*)(\d+)"
    r"|(\d+)\s*(?:comprimidos?|comp|cpr|cps|caps?|capsulas?|tabs?|tablets?)\b",
    re.IGNORECASE,
)

def extract_pack_size(s: str):
    m = _PACK_SIZE_CAPTURE_RE.search(s)
    if not m:
        return None
    val = m.group(1) or m.group(2)
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def strip_pack_noise(s: str) -> str:
    return _PACK_NOISE_RE.sub(" ", s)

_STRENGTH_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|ml|ui|iu)\s*(?:/\s*(\d+(?:[.,]\d+)?)\s*ml)?|(\d+(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)

def extract_strength(s: str):
    """Returns (clean_string_without_strength, strength_key) where
    strength_key is a normalised comparable token, or None if no strength
    was found. mg/mcg/g are converted to a common mg float; % and ratio
    (e.g. 120mg/5ml) forms are kept as a normalised string."""
    m = _STRENGTH_RE.search(s)
    if not m:
        return s, None
    clean = s[: m.start()] + " " + s[m.end():]
    if m.group(4):  # percent form
        try:
            val = float(m.group(4).replace(",", "."))
        except ValueError:
            return clean, None
        return clean, ("pct", round(val, 3))
    num_str, unit, ratio_ml = m.group(1), m.group(2), m.group(3)
    try:
        val = float(num_str.replace(",", "."))
    except ValueError:
        return clean, None
    unit = unit.lower()
    if ratio_ml:
        try:
            ratio_val = float(ratio_ml.replace(",", "."))
        except ValueError:
            ratio_val = None
        return clean, ("ratio", round(val, 3), unit, ratio_val)
    if unit == "g":
        val *= 1000
        unit = "mg"
    elif unit == "mcg":
        pass  # keep distinct from mg -- 500mcg != 500mg
    return clean, ("abs", round(val, 3), unit)

_FORM_KEYWORDS = {
    "comprimido": ["comprimido", "comprimidos", "comp", "cpr", "cp", "tablet", "tablets", "tab", "tabs"],
    "capsula": ["capsula", "capsulas", "cápsula", "cap", "caps"],
    "xarope": ["xarope", "syrup", "suspensao", "suspensão", "suspension"],
    "injetavel": ["injetavel", "injetável", "injecao", "injeccao", "injection", "inj", "ampola", "ampoula"],
    "creme": ["creme", "cream", "pomada", "gel", "ointment"],
    "gotas": ["gotas", "drops"],
    "saqueta": ["saqueta", "sache", "sachet"],
    "supositorio": ["supositorio", "supositório", "suppository"],
    "inalador": ["inalador", "inhaler", "spray"],
    "efervescente": ["comprimido efervescente", "efervescente", "effervescent"],
}
_FORM_LOOKUP = {kw: canon for canon, kws in _FORM_KEYWORDS.items() for kw in kws}
_FORM_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in _FORM_LOOKUP), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

def extract_form(s: str):
    m = _FORM_RE.search(s.lower())
    if not m:
        return s, None
    canon = _FORM_LOOKUP[m.group(1).lower()]
    clean = s[: m.start()] + " " + s[m.end():]
    return clean, canon

_PHONETIC_SUBS = [
    ("ph", "f"),
    ("kh", "k"),
    ("th", "t"),
    ("qu", "k"),
    ("q", "k"),
    ("c", "k"),
    ("y", "i"),
    ("w", "u"),
]

def phonetic_unify(s: str) -> str:
    for a, b in _PHONETIC_SUBS:
        s = s.replace(a, b)
    return s

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

def basic_clean(s: str) -> str:
    s = str(s or "")
    s = s.strip().lower()
    s = _unify_digits(s)
    s = _strip_combining_marks(s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s

def normalize_for_alias(s: str) -> str:
    """The alias-dictionary lookup key: normalised surface form, but NOT
    transliterated -- an Arabic-script alias like "بنادول" is stored and
    looked up as itself, because that is exactly the string a real
    pharmacist will type again next time."""
    return basic_clean(s)

def get_drug_name_core(raw: str):
    """Strip pack noise, strength and form out of a raw string, transliterate
    Arabic script, unify spelling variants, and return the residual
    'drug-name core' plus the structured fields extracted along the way.
    This is what rung 4 (fuzzy) and rung 6 (semantic) compare, per the
    brief's explicit instruction to strip strength/form/pack first."""
    s = str(raw or "")
    was_arabic = contains_arabic(s)
    s = transliterate_arabic(s)
    s = strip_pack_noise(s)
    pack_size = extract_pack_size(str(raw or ""))
    s, strength_key = extract_strength(s)
    s, form_key = extract_form(s)
    core = basic_clean(s)
    core = phonetic_unify(core)
    core = _WS_RE.sub(" ", core).strip()
    return {
        "core": core,
        "strength": strength_key,
        "form": form_key,
        "pack": pack_size,
        "was_arabic": was_arabic,
    }

# ===========================================================================
# Fuzzy + semantic scoring
# ===========================================================================

def fuzzy_ratio(a: str, b: str) -> float:
    """0-100 token-set-style similarity. rapidfuzz if available, else a
    difflib fallback scaled to the same range."""
    if not a or not b:
        return 0.0
    if HAVE_RAPIDFUZZ:
        return _rf_fuzz.token_set_ratio(a, b)
    # difflib fallback: token-set approximation by sorting+deduping tokens
    ta = " ".join(sorted(set(a.split())))
    tb = " ".join(sorted(set(b.split())))
    return difflib.SequenceMatcher(None, ta, tb).ratio() * 100

def char_ngrams(s: str, n: int = NGRAM_SIZE):
    s = f"  {s}  "
    if len(s) < n:
        return [s]
    return [s[i:i + n] for i in range(len(s) - n + 1)]

class NgramTfidfIndex:
    """Minimal, dependency-free TF-IDF over character n-grams, fit on the
    Index's own drug-name cores. Pure standard library (Counter + math.log)
    -- no sklearn, no model download, matching the brief's requirement that
    the 'semantic' rung not require network access."""

    def __init__(self, documents):
        self.n_docs = len(documents)
        df = Counter()
        self._doc_vectors = []
        for doc in documents:
            grams = set(char_ngrams(doc))
            for g in grams:
                df[g] += 1
        self.idf = {g: math.log((1 + self.n_docs) / (1 + c)) + 1 for g, c in df.items()}

    def vector(self, text: str):
        grams = char_ngrams(text)
        tf = Counter(grams)
        vec = {}
        for g, count in tf.items():
            idf = self.idf.get(g)
            if idf is None:
                continue
            vec[g] = count * idf
        return vec

    @staticmethod
    def cosine(vec_a, vec_b) -> float:
        if not vec_a or not vec_b:
            return 0.0
        common = set(vec_a) & set(vec_b)
        dot = sum(vec_a[g] * vec_b[g] for g in common)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def similarity(self, text_a: str, text_b: str) -> float:
        va = self.vector(text_a)
        vb = self.vector(text_b)
        return self.cosine(va, vb) * 100

# ===========================================================================
# Column detection
# ===========================================================================

INDEX_COLUMN_ALIASES = {
    "id": ["id", "sku", "code", "codigo", "código", "cod", "product_id", "index_id", "item_code"],
    "inn": ["inn", "dci", "principio_ativo", "princípio_ativo", "substancia_ativa", "substância_ativa",
            "ingrediente_ativo", "generic_name", "active_ingredient", "dcb"],
    "brand": ["brand", "nome_comercial", "nome", "product_name", "produto", "marca", "name", "descricao",
              "descrição", "commercial_name"],
    "form": ["form", "forma", "forma_farmaceutica", "forma_farmacêutica", "dosage_form", "apresentacao_forma"],
    "strength": ["strength", "dosagem", "dosage", "concentracao", "concentração", "dose"],
    "pack": ["pack", "pack_size", "embalagem", "tamanho_embalagem", "qtd_embalagem", "pack_qty", "apresentacao"],
    "manufacturer": ["manufacturer", "fabricante", "laboratorio", "laboratório", "lab", "vendor", "supplier"],
    "barcode": ["barcode", "ean", "ean13", "codigo_barras", "código_barras", "cod_barras", "gtin"],
    "price": ["price", "preco", "preço", "pvp", "valor", "price_mzn", "unit_price"],
}

ITEMS_COLUMN_ALIASES = {
    "item_id": ["item_id", "id", "sku", "row_id", "line_id"],
    "raw_name": ["raw_name", "name", "nome", "produto", "item", "description", "descricao", "descrição",
                 "product_name", "nome_produto", "designacao", "designação", "item_name"],
    "barcode": ["barcode", "ean", "ean13", "codigo_barras", "código_barras", "cod_barras", "gtin"],
    "price": ["price", "preco", "preço", "pvp", "valor", "unit_price", "price_mzn"],
    "pharmacy_id": ["pharmacy_id", "pharmacy", "farmacia", "farmácia", "loja", "store_id", "store"],
    "qty": ["qty", "quantidade", "qtd", "stock", "estoque", "quantity"],
}

def _norm_header(h: str) -> str:
    h = basic_clean(str(h))
    h = h.replace(" ", "_")
    return h

def detect_columns(df_columns, alias_map, required):
    normalized = {_norm_header(c): c for c in df_columns}
    detected = {}
    for canon, aliases in alias_map.items():
        found = None
        for alias in aliases:
            if alias in normalized:
                found = normalized[alias]
                break
        if not found:
            # substring fallback
            for norm_h, orig_h in normalized.items():
                if any(alias in norm_h for alias in aliases):
                    found = orig_h
                    break
        detected[canon] = found
    missing_required = [c for c in required if not detected.get(c)]
    if missing_required:
        raise SystemExit(
            f"ERROR: could not detect required column(s) {missing_required} in "
            f"columns {list(df_columns)}. Rename a column to match one of the "
            f"recognised aliases (see ITEMS_COLUMN_ALIASES / INDEX_COLUMN_ALIASES "
            f"at the top of match.py) and re-run."
        )
    return detected

# ===========================================================================
# Alias dictionary I/O
# ===========================================================================

def load_aliases(path):
    if not path or not os.path.exists(path):
        return {"version": 1, "aliases": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("version", 1)
    data.setdefault("aliases", {})
    return data

def save_aliases(path, data):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

def alias_learn(aliases_data, key, index_id, source="auto", today=None):
    today = today or date.today().isoformat()
    entry = aliases_data["aliases"].get(key)
    if entry and entry.get("index_id") == index_id:
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = today
    else:
        aliases_data["aliases"][key] = {
            "index_id": index_id,
            "count": 1,
            "first_seen": today,
            "last_seen": today,
            "source": source,
        }

# ===========================================================================
# Core matching
# ===========================================================================

def strengths_conflict(a, b):
    if a is None or b is None:
        return False  # absent, not conflicting -- handled separately
    if a[0] != b[0]:
        return True
    if a[0] == "pct":
        return abs(a[1] - b[1]) > max(a[1], b[1]) * STRENGTH_TOLERANCE_PCT + 1e-9
    if a[0] == "abs":
        if a[2] != b[2]:
            return True
        denom = max(a[1], b[1], 1e-9)
        return abs(a[1] - b[1]) / denom > STRENGTH_TOLERANCE_PCT
    if a[0] == "ratio":
        if a[2] != b[2]:
            return True
        denom = max(a[1], b[1], 1e-9)
        if abs(a[1] - b[1]) / denom > STRENGTH_TOLERANCE_PCT:
            return True
        if a[3] and b[3]:
            denom2 = max(a[3], b[3], 1e-9)
            return abs(a[3] - b[3]) / denom2 > STRENGTH_TOLERANCE_PCT
        return False
    return False

def strengths_agree(a, b):
    return a is not None and b is not None and not strengths_conflict(a, b)

class Matcher:
    def __init__(self, index_df, index_cols):
        self.index_cols = index_cols
        self.rows = []
        for _, r in index_df.iterrows():
            inn = str(r[index_cols["inn"]]) if index_cols.get("inn") else ""
            brand = str(r[index_cols["brand"]]) if index_cols.get("brand") else ""
            form_raw = str(r[index_cols["form"]]) if index_cols.get("form") else ""
            strength_raw = str(r[index_cols["strength"]]) if index_cols.get("strength") else ""
            pack_raw = r[index_cols["pack"]] if index_cols.get("pack") else None
            barcode = str(r[index_cols["barcode"]]) if index_cols.get("barcode") and pd.notna(r[index_cols["barcode"]]) else ""
            price = r[index_cols["price"]] if index_cols.get("price") and pd.notna(r[index_cols["price"]]) else None
            _, strength_key = extract_strength(strength_raw) if strength_raw and strength_raw != "nan" else ("", None)
            if strength_key is None and strength_raw and strength_raw != "nan":
                # strength column may already just be a bare number+unit; retry
                # against "x <value>" pattern to be permissive
                _, strength_key = extract_strength(strength_raw + " ")
            _, form_key = extract_form(form_raw) if form_raw and form_raw != "nan" else ("", None)
            if form_key is None and form_raw and form_raw.strip() and form_raw != "nan":
                form_key = basic_clean(form_raw)
            try:
                pack_val = int(float(pack_raw)) if pack_raw not in (None, "", "nan") else None
            except (TypeError, ValueError):
                pack_val = None
            brand_core = get_drug_name_core(brand)["core"]
            inn_core = get_drug_name_core(inn)["core"]
            self.rows.append({
                "id": str(r[index_cols["id"]]),
                "inn": inn, "brand": brand,
                "form_raw": form_raw, "strength_raw": strength_raw, "pack_raw": pack_raw,
                "barcode": BARCODE_STRIP_RE.sub("", barcode) if barcode and barcode != "nan" else "",
                "price": float(price) if price is not None else None,
                "strength_key": strength_key,
                "form_key": form_key,
                "pack_val": pack_val,
                "brand_core": brand_core,
                "inn_core": inn_core,
            })
        self.by_barcode = defaultdict(list)
        for row in self.rows:
            if row["barcode"]:
                self.by_barcode[row["barcode"]].append(row)
        corpus = [r["brand_core"] for r in self.rows] + [r["inn_core"] for r in self.rows]
        self.ngram_index = NgramTfidfIndex(corpus)

    def candidates_by_barcode(self, barcode):
        if not barcode:
            return []
        key = BARCODE_STRIP_RE.sub("", str(barcode))
        if not key:
            return []
        return self.by_barcode.get(key, [])

    def score_all(self, item_core_info, item_price=None):
        """Returns a list of (index_row, score_breakdown) sorted by combined
        score descending."""
        core = item_core_info["core"]
        istrength = item_core_info["strength"]
        iform = item_core_info["form"]
        ipack = item_core_info["pack"]
        results = []
        for row in self.rows:
            # IMPORTANT: the primary text score compares against the BRAND
            # only, not the INN. Every brand of a given INN shares the exact
            # same inn_core string (e.g. "Brufen", "Nurofen" and "Ibuprofeno
            # Generico" all carry inn_core "ibuprofeno") -- token-set fuzzy
            # ratio treats a shared substring/subset as a ~perfect match, so
            # comparing against inn_core as an equal alternative would make
            # every same-INN brand tie at ~100 regardless of which specific
            # brand the pharmacist actually wrote, destroying the very
            # brand disambiguation this rung exists to do. inn_sim is kept
            # only as a weak, CAPPED fallback: it can rescue a bare-generic
            # name into the ASK queue (rung 5) but can never by itself
            # manufacture the certainty required for AUTO.
            brand_sim = fuzzy_ratio(core, row["brand_core"])
            inn_sim_raw = fuzzy_ratio(core, row["inn_core"])
            inn_sim = min(inn_sim_raw, NAME_SIM_ASK_MIN + 1)
            brand_sem = self.ngram_index.similarity(core, row["brand_core"])
            inn_sem = min(self.ngram_index.similarity(core, row["inn_core"]), SEMANTIC_ASK_MIN + 1)

            name_sim = max(brand_sim, inn_sim)
            sem_sim = max(brand_sem, inn_sem)
            text_score = max(name_sim, sem_sim)
            text_signal = "fuzzy" if name_sim >= sem_sim else "semantic"
            if core and core == row["brand_core"]:
                text_score = 100.0
                text_signal = "exact"

            bonus = 0.0
            conflict = False
            strength_status = "absent"
            form_status = "absent"
            pack_status = "absent"

            if istrength is not None and row["strength_key"] is not None:
                if strengths_conflict(istrength, row["strength_key"]):
                    bonus -= STRENGTH_CONFLICT_PENALTY
                    conflict = True
                    strength_status = "conflict"
                else:
                    bonus += STRENGTH_AGREE_BONUS
                    strength_status = "agree"

            if iform is not None and row["form_key"] is not None:
                if iform != row["form_key"]:
                    bonus -= FORM_CONFLICT_PENALTY
                    conflict = True
                    form_status = "conflict"
                else:
                    bonus += FORM_AGREE_BONUS
                    form_status = "agree"

            if ipack is not None and row["pack_val"] is not None:
                if ipack != row["pack_val"]:
                    bonus -= PACK_CONFLICT_PENALTY
                    pack_status = "conflict"
                else:
                    bonus += PACK_AGREE_BONUS
                    pack_status = "agree"

            price_status = "absent"
            if item_price is not None and row["price"] is not None and row["price"] > 0:
                ratio = max(item_price, row["price"]) / max(min(item_price, row["price"]), 1e-9)
                if ratio - 1 <= PRICE_PROXIMITY_PCT:
                    bonus += PRICE_AGREE_BONUS
                    price_status = "agree"
                elif ratio >= PRICE_WILD_MISMATCH_RATIO:
                    bonus -= PRICE_MISMATCH_PENALTY
                    price_status = "mismatch"

            # IMPORTANT: keep the ranking score uncapped. Clipping to 100 here
            # would let a mediocre text match plus several small bonuses tie
            # with (or even risk being ordered ahead of, on insertion order)
            # a genuine exact/high-similarity match that happened to pick up
            # fewer bonus points -- silently promoting the wrong brand. The
            # 0-100 "display_score" shown to the user is clipped separately,
            # after ranking is already decided.
            combined_raw = text_score + bonus
            display_score = max(0.0, min(100.0, combined_raw))
            full_structure = strength_status == "agree" and form_status == "agree" and pack_status == "agree"
            results.append((row, {
                "text_score": text_score,
                "text_signal": text_signal,
                "combined": combined_raw,
                "display_score": display_score,
                "conflict": conflict,
                "strength_status": strength_status,
                "form_status": form_status,
                "pack_status": pack_status,
                "price_status": price_status,
                "has_structure_agree": strength_status == "agree" or (form_status == "agree" and pack_status == "agree"),
                "full_structure": full_structure,
            }))
        results.sort(key=lambda t: (t[1]["combined"], t[1]["text_score"]), reverse=True)
        return results


def _identity_key(row):
    """Groups Index SKU rows that are the same drug identity (INN, brand,
    form, strength) and differ only by pack size. Two different pack sizes
    of the same brand/strength are NOT the clinical danger this tool exists
    to prevent (that danger is Amoxil 500 vs Amoxil 250, i.e. a strength or
    product mismatch) -- so pack-only ambiguity should not by itself block
    an otherwise-confident name+strength match, it should just leave the
    exact pack size to resolve separately."""
    return (row["inn"], row["brand"], row["form_key"] or row["form_raw"], row["strength_key"] or row["strength_raw"])

def _group_candidates(scored, item_price=None):
    """Collapses raw per-SKU scored candidates into one entry per clinical
    identity. Returns a list of (representative_row, best_info, members)
    sorted by the group's best combined score, where `best_info` is the
    best-scoring member's info (used for all threshold decisions --
    text/strength/form are identical across siblings by construction) and
    `representative_row` is chosen deliberately: exact pack match if the
    item specified one, else nearest by price, else the smallest pack (a
    stable, explainable default) so the report always names one concrete
    Index row."""
    groups = defaultdict(list)
    for row, info in scored:
        groups[_identity_key(row)].append((row, info))
    grouped = []
    for key, members in groups.items():
        members.sort(key=lambda t: (t[1]["combined"], t[1]["text_score"]), reverse=True)
        best_info = members[0][1]
        pack_agree = [(r, i) for r, i in members if i["pack_status"] == "agree"]
        if pack_agree:
            rep_row = pack_agree[0][0]
        elif item_price is not None:
            priced = [(r, i) for r, i in members if r["price"] is not None]
            rep_row = min(priced, key=lambda t: abs(t[0]["price"] - item_price))[0] if priced else members[0][0]
        else:
            rep_row = min(
                members, key=lambda t: (t[0]["pack_val"] if t[0]["pack_val"] is not None else 10 ** 9, t[0]["id"])
            )[0]
        grouped.append((rep_row, best_info, members))
    grouped.sort(key=lambda t: (t[1]["combined"], t[1]["text_score"]), reverse=True)
    return grouped

def decide(item, matcher, aliases_data):
    """Runs one pharmacy item through the full ladder and returns a decision
    dict: status (AUTO/ASK/UNMATCHED), signal, score, matched row (if any),
    and top-K candidates for reporting."""
    raw_name = item["raw_name"]
    barcode = item.get("barcode")
    price = item.get("price")

    # --- Rung 1 (assisted by barcode, the strongest signal of all) -------
    bc_candidates = matcher.candidates_by_barcode(barcode)
    if len(bc_candidates) == 1:
        row = bc_candidates[0]
        return {
            "status": "AUTO", "signal": "barcode", "score": 100.0,
            "matched": row, "candidates": [(row, 100.0)],
            "reason": "exact barcode match",
        }

    core_info = get_drug_name_core(raw_name)
    alias_key = normalize_for_alias(raw_name)
    alias_entry = aliases_data["aliases"].get(alias_key)

    scored = matcher.score_all(core_info, item_price=price)
    if bc_candidates:  # ambiguous barcode (shared across rows) -- restrict, don't trust blindly
        bc_ids = {r["id"] for r in bc_candidates}
        scored = [t for t in scored if t[0]["id"] in bc_ids] or scored

    # Rank by CLINICAL IDENTITY (inn+brand+form+strength), not by raw SKU
    # row. Two rows that differ only by pack size are the same drug -- that
    # is a stock-keeping detail, not the strength/brand danger this tool
    # exists to prevent -- so they must not compete against each other in
    # the ambiguity margin check, and not clutter the human's 3 candidates
    # with 3 pack sizes of one identical product.
    groups = _group_candidates(scored, item_price=price)
    top = [(r, i["display_score"]) for r, i, _ in groups[:TOP_K_CANDIDATES]]
    best_row, best_info = (groups[0][0], groups[0][1]) if groups else (None, None)
    second_info = groups[1][1] if len(groups) > 1 else None
    second_row = groups[1][0] if len(groups) > 1 else None
    # margin uses the uncapped ranking score so a genuine exact/high-quality
    # match is never lost to a display-score tie against a weaker candidate
    # that happened to collect the same bonuses.
    margin = (best_info["combined"] - second_info["combined"]) if (best_info and second_info) else 999
    # Two DIFFERENT brands of the same INN, at the same strength/form/pack,
    # are indistinguishable from a short or generic name alone (e.g. an
    # "AMO 500mg" abbreviation could be Amoxil, Clamoxyl or generic
    # Amoxicilina) -- getting the brand wrong still means the wrong
    # manufacturer/price is shown, so demand a much wider margin here
    # before trusting the name to pick one over the other.
    if second_row is not None and second_row["inn"] == best_row["inn"] and second_info["full_structure"]:
        margin_required = MIN_SCORE_MARGIN_FOR_AUTO_SAME_INN
    else:
        margin_required = MIN_SCORE_MARGIN_FOR_AUTO

    # --- Rung 3a: alias dictionary, high-confidence branch only -----------
    # A well-confirmed alias is an independent, strong signal and can win
    # outright here. A LOW-count alias, by contrast, must not be allowed to
    # downgrade a row that would already auto-match cleanly on its own
    # merits (name+structure) below -- e.g. the second time in one file
    # that "CLAMOXYL 500MG" appears, it has already been self-taught into
    # the dictionary with count=1 by the first occurrence a few rows
    # earlier, but that must never make the SECOND occurrence *less*
    # certain than an exact string+strength match already is. So the
    # low-count alias branch (3b) only runs further below, as a fallback.
    alias_row, alias_conflict = None, False
    if alias_entry:
        alias_row = next((r for r in matcher.rows if r["id"] == alias_entry["index_id"]), None)
        if alias_row is not None:
            if core_info["strength"] is not None and alias_row["strength_key"] is not None:
                alias_conflict = strengths_conflict(core_info["strength"], alias_row["strength_key"])
            if core_info["form"] is not None and alias_row["form_key"] is not None:
                alias_conflict = alias_conflict or (core_info["form"] != alias_row["form_key"])
            if alias_entry.get("count", 0) >= ALIAS_AUTO_MIN_COUNT and not alias_conflict:
                return {
                    "status": "AUTO", "signal": "alias_frequency", "score": 90.0 + min(alias_entry["count"], 10),
                    "matched": alias_row,
                    "candidates": [(alias_row, 95.0)] + [(r, s) for r, s in top if r["id"] != alias_row["id"]][:2],
                    "reason": f"alias '{raw_name}' confirmed {alias_entry['count']}x previously -> {alias_row['id']}",
                }

    if not best_row:
        return {"status": "UNMATCHED", "signal": "none", "score": 0.0, "matched": None,
                "candidates": [], "reason": "empty index or empty name"}

    # --- Rungs 2/4/5/6 combined: name (+semantic) plus structure ----------
    # Two different bars, both requiring zero conflicts and a safe margin:
    #  - full 3-way structure agreement (strength+form+pack) needs only a
    #    modest name score, because that much structural agreement rarely
    #    happens by coincidence (rung 5 carrying the match);
    #  - partial structure (strength alone, typically) needs a strong name
    #    score too, since strength alone repeats across many products.
    name_bar = NAME_SIM_AUTO_MIN_FULL_STRUCTURE if best_info["full_structure"] else NAME_SIM_AUTO_MIN
    if (
        best_info["text_score"] >= name_bar
        and best_info["has_structure_agree"]
        and not best_info["conflict"]
        and margin >= margin_required
    ):
        return {
            "status": "AUTO", "signal": f"name+structure ({best_info['text_signal']})",
            "score": round(best_info["display_score"], 1), "matched": best_row,
            "candidates": top,
            "reason": (
                f"name similarity {best_info['text_score']:.0f} "
                f"+ strength/form/pack agree, margin {margin:.0f} over runner-up"
            ),
        }

    # --- Rung 3b: low-count alias, fallback only ---------------------------
    # Reached only because the row did NOT already clear name+structure
    # AUTO above. A once- or twice-seen alias is still worth bubbling to
    # the top of the human's candidate list, even if it cannot yet be
    # trusted alone.
    if alias_row is not None and not alias_conflict:
        boosted = [(alias_row, 80.0)] + [(r, s) for r, s in top if r["id"] != alias_row["id"]]
        return {
            "status": "ASK", "signal": "alias_low_count", "score": 80.0,
            "matched": None, "candidates": boosted[:TOP_K_CANDIDATES],
            "reason": f"alias '{raw_name}' seen only {alias_entry.get('count', 0)}x so far -> needs one more confirmation",
        }

    # --- Otherwise: ASK if there is anything worth asking about, else UNMATCHED
    # A candidate is worth a human's time when EITHER the name/semantic text
    # score clears the review floor, OR full structural agreement alone
    # makes it plausible even though the text itself is weak (an
    # abbreviation like "VOL 1% GEL X1" -- rung 5 rescuing what would
    # otherwise be a silent UNMATCHED).
    ask_worthy = [
        (r, i) for r, i, _ in groups
        if i["text_score"] >= NAME_SIM_ASK_MIN
        or (i["text_score"] >= SEMANTIC_ASK_MIN and i["text_signal"] == "semantic")
        or (i["full_structure"] and not i["conflict"] and i["text_score"] >= 25)
    ]
    if ask_worthy:
        reasons = []
        if best_info["conflict"]:
            reasons.append("top candidate has a conflicting strength/form -- DO NOT auto-accept")
        if best_info["text_score"] >= name_bar and not best_info["has_structure_agree"]:
            reasons.append("name looks like a strong match but strength/pack is missing, so it cannot auto-match alone")
        if margin < margin_required and second_info:
            if margin_required == MIN_SCORE_MARGIN_FOR_AUTO_SAME_INN:
                reasons.append(
                    f"top two candidates are the same drug under different brands, only {margin:.0f} points "
                    f"apart -- which brand/manufacturer it is needs a human"
                )
            else:
                reasons.append(f"top two candidates are only {margin:.0f} points apart -- too close to guess")
        if best_info["full_structure"] and best_info["text_score"] < NAME_SIM_AUTO_MIN_FULL_STRUCTURE:
            reasons.append("strength/form/pack all agree but the name itself is too different to auto-accept")
        if not reasons:
            reasons.append("name/semantic similarity is in the medium-confidence band")
        return {
            "status": "ASK", "signal": best_info["text_signal"], "score": round(best_info["display_score"], 1),
            "matched": None, "candidates": [(r, i["display_score"]) for r, i in ask_worthy[:TOP_K_CANDIDATES]],
            "reason": "; ".join(reasons),
        }

    return {
        "status": "UNMATCHED", "signal": "none", "score": round(best_info["display_score"], 1) if best_info else 0.0,
        "matched": None, "candidates": [],
        "reason": "no candidate cleared even the human-review similarity floor",
    }

# ===========================================================================
# Report writing
# ===========================================================================

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, name="Arial")
BODY_FONT = Font(name="Arial")
AUTO_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
ASK_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
UNMATCHED_FILL = PatternFill(start_color="FCE4E4", end_color="FCE4E4", fill_type="solid")

def _write_header(ws, headers):
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"

def _autofit(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

def build_report(out_path, total, auto_rows, ask_rows, unmatched_rows, signal_counts, detected_index_cols, detected_item_cols):
    wb = Workbook()

    # --- Summary sheet -----------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Drug Name Matcher - Run Summary"
    ws["A1"].font = Font(bold=True, size=14, name="Arial")
    ws["A2"] = f"Generated: {date.today().isoformat()}"
    ws["A2"].font = BODY_FONT

    r = 4
    ws.cell(row=r, column=1, value="Detected columns - Index").font = Font(bold=True, name="Arial")
    r += 1
    for k, v in detected_index_cols.items():
        ws.cell(row=r, column=1, value=k).font = BODY_FONT
        ws.cell(row=r, column=2, value=v or "(not found)").font = BODY_FONT
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Detected columns - Pharmacy items").font = Font(bold=True, name="Arial")
    r += 1
    for k, v in detected_item_cols.items():
        ws.cell(row=r, column=1, value=k).font = BODY_FONT
        ws.cell(row=r, column=2, value=v or "(not found)").font = BODY_FONT
        r += 1

    r += 1
    stats_start = r
    ws.cell(row=r, column=1, value="Status").font = Font(bold=True, name="Arial")
    ws.cell(row=r, column=2, value="Count").font = Font(bold=True, name="Arial")
    ws.cell(row=r, column=3, value="% of total").font = Font(bold=True, name="Arial")
    r += 1
    total_row = r + 3
    for label, sheet_name in (("AUTO", "AUTO"), ("ASK", "ASK"), ("UNMATCHED", "UNMATCHED")):
        ws.cell(row=r, column=1, value=label).font = BODY_FONT
        # COUNTA on the sheet's item_id column (col A), minus 1 for the header row
        ws.cell(row=r, column=2, value=f"=COUNTA({sheet_name}!A:A)-1").font = BODY_FONT
        ws.cell(row=r, column=3, value=f"=B{r}/B{total_row}").font = BODY_FONT
        ws.cell(row=r, column=3).number_format = "0.0%"
        r += 1
    ws.cell(row=r, column=1, value="TOTAL").font = Font(bold=True, name="Arial")
    ws.cell(row=r, column=2, value=f"=SUM(B{stats_start+1}:B{r-1})").font = Font(bold=True, name="Arial")
    assert total_row == r
    r += 2

    ws.cell(row=r, column=1, value="AUTO matches by deciding signal").font = Font(bold=True, name="Arial")
    r += 1
    ws.cell(row=r, column=1, value="Signal").font = Font(bold=True, name="Arial")
    ws.cell(row=r, column=2, value="Count").font = Font(bold=True, name="Arial")
    ws.cell(row=r, column=3, value="% of AUTO").font = Font(bold=True, name="Arial")
    r += 1
    sig_start = r
    for sig, cnt in sorted(signal_counts.items(), key=lambda t: -t[1]):
        ws.cell(row=r, column=1, value=sig).font = BODY_FONT
        ws.cell(row=r, column=2, value=cnt).font = BODY_FONT
        ws.cell(row=r, column=3, value=f"=B{r}/$B${stats_start + 1}").font = BODY_FONT
        ws.cell(row=r, column=3).number_format = "0.0%"
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Decision guide (see README.md):").font = Font(italic=True, name="Arial")
    r += 1
    ws.cell(row=r, column=1, value="> 85% AUTO: integration is easy, proceed").font = BODY_FONT
    r += 1
    ws.cell(row=r, column=1, value="60-85% AUTO: workable, needs a seed dictionary").font = BODY_FONT
    r += 1
    ws.cell(row=r, column=1, value="< 60% AUTO: matching is a project of its own").font = BODY_FONT
    _autofit(ws, [42, 14, 12])

    # --- AUTO sheet ----------------------------------------------------------
    ws = wb.create_sheet("AUTO")
    headers = ["item_id", "pharmacy_id", "raw_name", "matched_index_id", "matched_brand", "matched_inn",
               "matched_form", "matched_strength", "matched_pack", "signal", "score", "reason"]
    _write_header(ws, headers)
    for i, row in enumerate(auto_rows, start=2):
        for c, key in enumerate(headers, start=1):
            cell = ws.cell(row=i, column=c, value=row.get(key, ""))
            cell.font = BODY_FONT
            cell.fill = AUTO_FILL
    _autofit(ws, [10, 12, 30, 12, 22, 20, 14, 14, 10, 22, 8, 50])

    # --- ASK sheet -----------------------------------------------------------
    ws = wb.create_sheet("ASK")
    headers = ["item_id", "pharmacy_id", "raw_name",
               "candidate_1_id", "candidate_1_name", "candidate_1_score",
               "candidate_2_id", "candidate_2_name", "candidate_2_score",
               "candidate_3_id", "candidate_3_name", "candidate_3_score",
               "reason", "human_decision_index_id"]
    _write_header(ws, headers)
    for i, row in enumerate(ask_rows, start=2):
        for c, key in enumerate(headers, start=1):
            cell = ws.cell(row=i, column=c, value=row.get(key, ""))
            cell.font = BODY_FONT
            cell.fill = ASK_FILL
    _autofit(ws, [10, 12, 30, 12, 22, 10, 12, 22, 10, 12, 22, 10, 50, 22])

    # --- UNMATCHED sheet -------------------------------------------------------
    ws = wb.create_sheet("UNMATCHED")
    headers = ["item_id", "pharmacy_id", "raw_name", "reason"]
    _write_header(ws, headers)
    for i, row in enumerate(unmatched_rows, start=2):
        for c, key in enumerate(headers, start=1):
            cell = ws.cell(row=i, column=c, value=row.get(key, ""))
            cell.font = BODY_FONT
            cell.fill = UNMATCHED_FILL
    _autofit(ws, [10, 12, 40, 60])

    wb.save(out_path)


def apply_resolutions(resolutions_path, aliases_data, matcher):
    """Rung 7: a human answers an ASK (or UNMATCHED) row, and that answer is
    written into the alias dictionary so the next pharmacy that types the
    same string auto-matches. CSV columns: raw_name (or item_id), index_id."""
    if not resolutions_path or not os.path.exists(resolutions_path):
        return 0
    df = pd.read_csv(resolutions_path, dtype=str).fillna("")
    cols = {c.lower(): c for c in df.columns}
    name_col = cols.get("raw_name") or cols.get("name")
    id_col = cols.get("index_id") or cols.get("matched_index_id")
    if not name_col or not id_col:
        print("WARNING: --resolutions file needs 'raw_name' and 'index_id' columns; skipping.")
        return 0
    valid_ids = {r["id"] for r in matcher.rows}
    learned = 0
    for _, row in df.iterrows():
        idx_id = row[id_col].strip()
        raw = row[name_col]
        if not idx_id or idx_id.lower() in ("reject", "none", "new") or idx_id not in valid_ids:
            continue
        key = normalize_for_alias(raw)
        alias_learn(aliases_data, key, idx_id, source="human")
        learned += 1
    return learned

# ===========================================================================
# Main
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description="Drug name matcher for pharmacy stock lists against a drug Index.")
    ap.add_argument("--items", required=True, help="Pharmacy items CSV")
    ap.add_argument("--index", required=True, help="Drug Index CSV")
    ap.add_argument("--aliases", default="aliases.json", help="Alias dictionary JSON (read + written back)")
    ap.add_argument("--out", default="report.xlsx", help="Output xlsx report path")
    ap.add_argument("--resolutions", default=None,
                     help="Optional CSV of human answers to previous ASK rows (columns: raw_name, index_id). "
                          "Each one is learned into the alias dictionary before this run's matching.")
    args = ap.parse_args()

    index_df = pd.read_csv(args.index, dtype=str)
    items_df = pd.read_csv(args.items, dtype=str)

    index_cols = detect_columns(index_df.columns, INDEX_COLUMN_ALIASES, required=["id", "brand"])
    item_cols = detect_columns(items_df.columns, ITEMS_COLUMN_ALIASES, required=["item_id", "raw_name"])

    print("Detected Index columns:")
    for k, v in index_cols.items():
        print(f"  {k:14s} -> {v}")
    print("Detected pharmacy-item columns:")
    for k, v in item_cols.items():
        print(f"  {k:14s} -> {v}")
    print()

    matcher = Matcher(index_df, index_cols)
    aliases_data = load_aliases(args.aliases)

    if args.resolutions:
        n = apply_resolutions(args.resolutions, aliases_data, matcher)
        print(f"Learned {n} human resolution(s) from {args.resolutions} into the alias dictionary.\n")

    auto_rows, ask_rows, unmatched_rows = [], [], []
    signal_counts = Counter()

    for _, r in items_df.iterrows():
        item = {
            "item_id": str(r[item_cols["item_id"]]),
            "pharmacy_id": str(r[item_cols["pharmacy_id"]]) if item_cols.get("pharmacy_id") and pd.notna(r[item_cols["pharmacy_id"]]) else "",
            "raw_name": str(r[item_cols["raw_name"]]) if pd.notna(r[item_cols["raw_name"]]) else "",
            "barcode": str(r[item_cols["barcode"]]) if item_cols.get("barcode") and pd.notna(r[item_cols["barcode"]]) else "",
            "price": None,
        }
        if item_cols.get("price") and pd.notna(r[item_cols["price"]]):
            try:
                item["price"] = float(r[item_cols["price"]])
            except ValueError:
                item["price"] = None

        decision = decide(item, matcher, aliases_data)

        if decision["status"] == "AUTO":
            row = decision["matched"]
            signal_counts[decision["signal"]] += 1
            auto_rows.append({
                "item_id": item["item_id"], "pharmacy_id": item["pharmacy_id"], "raw_name": item["raw_name"],
                "matched_index_id": row["id"], "matched_brand": row["brand"], "matched_inn": row["inn"],
                "matched_form": row["form_raw"], "matched_strength": row["strength_raw"], "matched_pack": row["pack_raw"],
                "signal": decision["signal"], "score": decision["score"], "reason": decision["reason"],
            })
            key = normalize_for_alias(item["raw_name"])
            alias_learn(aliases_data, key, row["id"], source="auto")
        elif decision["status"] == "ASK":
            cand = decision["candidates"]
            padded = list(cand) + [(None, None)] * (TOP_K_CANDIDATES - len(cand))
            row_out = {"item_id": item["item_id"], "pharmacy_id": item["pharmacy_id"], "raw_name": item["raw_name"],
                       "reason": decision["reason"], "human_decision_index_id": ""}
            for i in range(TOP_K_CANDIDATES):
                r_, score_ = padded[i]
                row_out[f"candidate_{i+1}_id"] = r_["id"] if r_ else ""
                row_out[f"candidate_{i+1}_name"] = f"{r_['brand']} {r_['strength_raw']} {r_['form_raw']} (pack {r_['pack_raw']})" if r_ else ""
                row_out[f"candidate_{i+1}_score"] = round(score_, 1) if score_ is not None else ""
            ask_rows.append(row_out)
        else:
            unmatched_rows.append({
                "item_id": item["item_id"], "pharmacy_id": item["pharmacy_id"], "raw_name": item["raw_name"],
                "reason": decision["reason"],
            })

    save_aliases(args.aliases, aliases_data)

    total = len(items_df)
    n_auto, n_ask, n_unmatched = len(auto_rows), len(ask_rows), len(unmatched_rows)

    build_report(args.out, total, auto_rows, ask_rows, unmatched_rows, signal_counts, index_cols, item_cols)

    print("=" * 60)
    print(f"Total rows:      {total}")
    print(f"AUTO:            {n_auto:4d}  ({100*n_auto/total:5.1f}%)")
    print(f"ASK:             {n_ask:4d}  ({100*n_ask/total:5.1f}%)")
    print(f"UNMATCHED:       {n_unmatched:4d}  ({100*n_unmatched/total:5.1f}%)")
    print("-" * 60)
    print("AUTO matches by deciding signal:")
    for sig, cnt in sorted(signal_counts.items(), key=lambda t: -t[1]):
        print(f"  {sig:26s} {cnt:4d}  ({100*cnt/max(n_auto,1):5.1f}% of AUTO)")
    print("=" * 60)
    print(f"Report written to: {args.out}")
    print(f"Alias dictionary written back to: {args.aliases} "
          f"({len(aliases_data['aliases'])} aliases now known)")
    if not HAVE_RAPIDFUZZ:
        print("NOTE: rapidfuzz not installed -- using the stdlib difflib fallback for fuzzy scoring.")


if __name__ == "__main__":
    main()
