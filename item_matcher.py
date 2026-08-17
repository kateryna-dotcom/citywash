"""
Matches a line item parsed off a supplier invoice to the corresponding item
in Kateryna's Cash On Tab catalogue, so the purchase document can be filled
in without her hunting for each product by hand.

Suppliers use their own SKUs and their own wording for the same product, so
matching runs in three tiers, best first:

  1. A learned mapping she has already confirmed for this
     (supplier, supplier_sku) pair -- always wins, never asks twice.
  2. An exact hit on the catalogue's barcode or item code. Some suppliers
     (e.g. victoriascent) print the manufacturer barcode that Cash On Tab
     also stores, so this alone resolves whole invoices.
  3. Fuzzy match on the product name, scoring both overall string similarity
     and how many of the invoice's words appear in the catalogue name.
     Returns ranked candidates rather than one answer.

Anything that isn't tier 1 or 2, or whose top fuzzy candidate isn't clearly
ahead of the runner-up, is returned as `needs_confirmation` -- the item is
still proposed (best guess first), just flagged for her to confirm, matching
how price mismatches already behave in the מלאי review UI.
"""
import difflib
import json
import os
import re

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "cashontab_catalog.json")

# Above this score a fuzzy name match is good enough to propose confidently,
# provided it's also clearly ahead of the next candidate (see _MIN_MARGIN).
_CONFIDENT_SCORE = 0.75
# If the runner-up is within this of the winner, the choice is ambiguous
# (e.g. the same wiper blade in sizes 8"/9"/14") -- ask rather than guess.
_MIN_MARGIN = 0.06

_catalog = None
_by_barcode = None
_by_code = None
_normalized_names = None


def _normalize(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"[^\w֐-׿]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# Invisible Unicode bidi-control marks (LRM/RLM/LRE/RLE/PDF/LRI/RLI/FSI/PDI)
# that pypdf sometimes leaves stuck to a token when extracting text next to
# Hebrew -- invisible on screen, but they break exact string equality, e.g.
# "CW010010" straight from the catalogue vs. "‎CW010010" straight off
# a PDF look identical but aren't ==. Strip them (and stray whitespace)
# before ever comparing a barcode/code against the catalogue.
_BIDI_MARKS_RE = re.compile("[‎‏‪-‮⁦-⁩]")


def _clean_key(s: str) -> str:
    return _BIDI_MARKS_RE.sub("", str(s or "")).strip()


# Public alias -- web_app.py uses this to clean a stored line item's sku
# before using it as an item_mappings key, so a mapping confirmed today
# still matches the same product on a future invoice.
clean_key = _clean_key


def _load():
    """Loads and indexes the catalogue once per process."""
    global _catalog, _by_barcode, _by_code, _normalized_names
    if _catalog is not None:
        return
    with open(_CATALOG_PATH, encoding="utf-8") as f:
        _catalog = json.load(f)
    _by_barcode, _by_code, _normalized_names = {}, {}, []
    for item in _catalog:
        if item.get("barcode"):
            _by_barcode.setdefault(_clean_key(item["barcode"]), item)
        if item.get("code"):
            _by_code.setdefault(_clean_key(item["code"]), item)
        _normalized_names.append((_normalize(item.get("name")), item))


def catalog_size() -> int:
    _load()
    return len(_catalog)


def _score(query_norm: str, query_tokens: set, candidate_norm: str) -> float:
    if not candidate_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    tokens = set(candidate_norm.split())
    overlap = len(query_tokens & tokens) / max(1, len(query_tokens))
    return 0.5 * ratio + 0.5 * overlap


def search_by_name(name: str, limit: int = 5) -> list:
    """Ranked catalogue candidates for a free-text product name."""
    _load()
    query_norm = _normalize(name)
    if not query_norm:
        return []
    query_tokens = set(query_norm.split())
    scored = [
        (_score(query_norm, query_tokens, cand_norm), item)
        for cand_norm, item in _normalized_names
    ]
    scored.sort(key=lambda pair: -pair[0])
    return [
        {**item, "score": round(score, 3)}
        for score, item in scored[:limit]
    ]


def _exact(key: str):
    _load()
    key = _clean_key(key)
    if not key:
        return None
    return _by_barcode.get(key) or _by_code.get(key)


def lookup(key: str) -> dict:
    """Public exact lookup by barcode or catalogue item code -- used when
    Kateryna types/scans a barcode herself to resolve an item the automatic
    matcher couldn't confidently place."""
    return _exact(key)


def match_item(line_item: dict, learned_mapping: dict = None) -> dict:
    """Resolves one parsed invoice line to a catalogue item.

    `learned_mapping` is the previously-confirmed mapping for this
    (supplier, sku) pair, if any -- pass None when there isn't one.

    Returns the line item plus:
        matched_code / matched_barcode / matched_name -- the proposal
        match_source  -- "learned" | "exact" | "fuzzy" | "none"
        needs_confirmation -- True when she should eyeball it
        candidates    -- ranked alternatives, for the picker UI
    """
    result = dict(line_item)
    # PDF text extraction sometimes leaves invisible bidi-control marks
    # stuck to the sku/barcode token, which look identical on screen but
    # break exact equality against the (clean) catalogue -- strip them here
    # so both the matching below and anything stored/displayed downstream
    # (item_mappings, the review UI) use the clean value.
    if result.get("sku"):
        result["sku"] = _clean_key(result["sku"])
    if result.get("barcode"):
        result["barcode"] = _clean_key(result["barcode"])
    result.update({
        "matched_code": None, "matched_barcode": None, "matched_name": None,
        "match_source": "none", "needs_confirmation": True, "candidates": [],
    })

    if learned_mapping:
        action = learned_mapping.get("action", "matched")
        if action == "skip":
            # Kateryna has said this line should never be entered into
            # מלАי (e.g. a fee line, or something she doesn't stock).
            result.update({"match_source": "skip", "needs_confirmation": False})
            return result
        if action == "new_pending":
            # A real product, confirmed by her, but not yet in the Cash On
            # Tab catalogue -- stays flagged until she adds it there and
            # re-resolves it with a barcode.
            result.update({
                "match_source": "new_pending", "needs_confirmation": True,
                "matched_name": learned_mapping.get("catalog_name"),
                "notes": learned_mapping.get("notes"),
            })
            return result
        if learned_mapping.get("catalog_code"):
            item = _exact(learned_mapping["catalog_code"]) or {}
            result.update({
                "matched_code": learned_mapping["catalog_code"],
                "matched_barcode": item.get("barcode"),
                "matched_name": item.get("name") or learned_mapping.get("catalog_name"),
                "match_source": "learned", "needs_confirmation": False,
            })
            return result

    for key in (line_item.get("sku"), line_item.get("barcode")):
        item = _exact(key)
        if item:
            result.update({
                "matched_code": item["code"], "matched_barcode": item["barcode"],
                "matched_name": item["name"],
                "match_source": "exact", "needs_confirmation": False,
            })
            return result

    candidates = search_by_name(line_item.get("description", ""))
    if not candidates:
        return result

    top = candidates[0]
    runner_up_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    clear_winner = (top["score"] - runner_up_score) >= _MIN_MARGIN
    result.update({
        "matched_code": top["code"], "matched_barcode": top["barcode"],
        "matched_name": top["name"], "match_source": "fuzzy",
        "needs_confirmation": not (top["score"] >= _CONFIDENT_SCORE and clear_winner),
        "candidates": candidates,
    })
    return result


def match_invoice_items(line_items: list, learned_mappings: dict = None) -> list:
    """Same as match_item over a whole invoice. `learned_mappings` maps
    supplier_sku -> mapping row for the invoice's supplier."""
    learned_mappings = learned_mappings or {}
    out = []
    for line_item in line_items or []:
        key = _clean_key(line_item.get("sku") or line_item.get("barcode") or "")
        out.append(match_item(line_item, learned_mappings.get(key)))
    return out
