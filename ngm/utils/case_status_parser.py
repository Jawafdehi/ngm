"""Shared parser for the court ``case_status`` free-text field.

The court portals stuff one overloaded string into ``case_status``. Today every
spider re-parses it differently (or not at all), so the structured columns are
mostly empty and one Supreme column-header leaks in as a status. This module is
the single source of truth: every listing/enrichment spider AND the one-time
backfill import it, so scraped rows and historical rows are normalised
identically and can never drift.

Three real shapes seen in the data (counts from the 1.6M-row audit):

1. arrow      ``फैसला / अन्तिम आदेश >> <outcome>``   (829,307) — outcome enumerable
2. paren-date ``फैसला (मिती: YYYY/MM/DD)``           (465,733; High/Supreme/Special)
3. pending    ``चालु`` / ``चलिरहेको`` / ...          (~204,539)
4. invalid    ``आदेश /फ␣फैसलाको किसिम``               (103,212 Supreme) — a scraped header

The parser returns a :class:`ParsedCaseStatus`; call sites write the typed
columns from it instead of clipping the raw string to ``[:100]``.
"""

from dataclasses import dataclass
from datetime import date
import re

from ngm.utils.db_helpers import convert_bs_to_ad
from ngm.utils.normalizer import normalize_whitespace, normalize_date


# --- enums (plain string constants; kept flat for DB enum + JSON friendliness) ---

# lifecycle_status
PENDING = "PENDING"
DECIDED = "DECIDED"
UNKNOWN = "UNKNOWN"

# verdict_type — criminal (charge) outcomes kept distinct from civil (claim) ones
CONVICTED = "CONVICTED"
ACQUITTED = "ACQUITTED"
PARTIALLY_CONVICTED = "PARTIALLY_CONVICTED"
CLAIM_UPHELD = "CLAIM_UPHELD"
CLAIM_DENIED = "CLAIM_DENIED"
PARTIALLY_UPHELD = "PARTIALLY_UPHELD"
SETTLED = "SETTLED"
WITHDRAWN = "WITHDRAWN"
DISMISSED = "DISMISSED"
QUASHED = "QUASHED"
PROCEDURAL = "PROCEDURAL"
ABEYANCE = "ABEYANCE"
STRUCK_OFF = "STRUCK_OFF"
AMENDED = "AMENDED"
OTHER = "OTHER"


# --- vocabulary --------------------------------------------------------------

# Header/label cells that get mis-scraped into case_status (DQ-01). Compared
# space-insensitively (see _despace) because the portals vary the spacing.
_HEADER_LABELS = {
    "आदेश/फैसलाको किसिम",
    "मुद्दाको किसिम",
    "तारेखको किसिम",
    "फैसला/आदेशको किसिम",
}

_PENDING_VALUES = {
    "चालु",
    "चलिरहेको",
    "चली रहेको",
    "विचाराधीन",
}

# Right-hand side of ``… >> <outcome>`` → verdict_type. Keys are pre-normalised
# by _norm_outcome (व→ब unification, whitespace). Long tail falls through to
# OTHER and is flagged ``unmapped`` for the DQ metric.
_OUTCOME_MAP = {
    "माग बमोजिम हुने": CLAIM_UPHELD,
    "दाबी पुग्ने": CLAIM_UPHELD,
    "मिलापत्र": SETTLED,
    "दाबी नपुग्ने": CLAIM_DENIED,
    "अभियोग दाबी पुग्ने": CONVICTED,
    "मुद्दा फिर्ता": WITHDRAWN,
    "डिसमिस": DISMISSED,
    "आंशिक दाबी पुग्ने": PARTIALLY_UPHELD,
    "अधिकृत वारेसनामा प्रमाणित गरिएको": PROCEDURAL,
    "विवाह दर्ता गरिएको": PROCEDURAL,
    "माग बमोजिम नहुने": CLAIM_DENIED,
    "अभियोग दाबी नपुग्ने": ACQUITTED,
    "खारेज": QUASHED,
    "आंशिक अभियोग दाबी पुग्ने": PARTIALLY_CONVICTED,
    "लगत कट्टा गर्ने": STRUCK_OFF,
    "अन्य": OTHER,
    "मुल्तबिमा राख्ने": ABEYANCE,
    "तामेली": STRUCK_OFF,
    "संशोधन हुने": AMENDED,
    "कानून बमोजिम गर्नु": PROCEDURAL,
    "मुल्तवी जगाउने": PROCEDURAL,
}

# Final-hearing ``decision_type`` (from extra_data.enrichment_hearings) → verdict_type.
# Lets Special/Supreme cases resolve an outcome their case_status lacks. Only
# terminal decisions map; interlocutory ones (थुनछेक, साक्षी बुझ्ने, …) stay None.
_HEARING_DECISION_MAP = {
    "सफाई": ACQUITTED,
    "ठहर": CONVICTED,
    "आंशिक": PARTIALLY_CONVICTED,
}

_DECIDED_MARKERS = ("फैसला", "अन्तिम आदेश", "आदेश")

# A BS date token like 2081/09/28, २०८१-०९-२८, 2081।09।28 (any of / - . ।)
_DATE_TOKEN = re.compile(r"([०-९0-9]{4})\s*[/\-\.।]\s*([०-९0-9]{1,2})\s*[/\-\.।]\s*([०-९0-9]{1,2})")


@dataclass
class ParsedCaseStatus:
    """Structured view of a raw ``case_status`` string."""

    lifecycle_status: str = UNKNOWN
    verdict_type: str | None = None
    verdict_outcome_raw: str | None = None
    verdict_date_bs: str | None = None
    verdict_date_ad: date | None = None
    # True when the case is DECIDED via the arrow form but the outcome text was
    # not in _OUTCOME_MAP — surfaced as a data-quality metric, never silently lost.
    unmapped: bool = False


def _despace(text: str) -> str:
    return text.replace(" ", "")


def _norm_outcome(outcome: str) -> str:
    """Normalise an outcome phrase for map lookup.

    The portals spell the charge word both ``दावी`` and ``दाबी`` (व/ब) — unify to
    ``दाबी`` so both variants hit one key. Whitespace already collapsed upstream.
    """
    return normalize_whitespace(outcome).replace("दावी", "दाबी")


def _extract_verdict_date(text: str) -> tuple[str | None, date | None]:
    """Pull a BS verdict date out of ``… (मिती: YYYY/MM/DD)`` etc., if present."""
    m = _DATE_TOKEN.search(text)
    if not m:
        return None, None
    date_bs = normalize_date("-".join(m.groups()))
    return date_bs, convert_bs_to_ad(date_bs)


def parse_case_status(raw: str | None) -> ParsedCaseStatus:
    """Parse a raw ``case_status`` string into typed fields (rules R1–R5)."""
    value = normalize_whitespace(raw or "")
    if not value:
        return ParsedCaseStatus(lifecycle_status=UNKNOWN)

    # R1 — header/label artifact leaked in as a status (DQ-01): not a real status.
    if _despace(value) in {_despace(h) for h in _HEADER_LABELS}:
        return ParsedCaseStatus(lifecycle_status=UNKNOWN)

    # R2 — pending / ongoing, in any of its spellings (DQ-05).
    if value in _PENDING_VALUES:
        return ParsedCaseStatus(lifecycle_status=PENDING)

    date_bs, date_ad = _extract_verdict_date(value)

    # R3 — arrow form: outcome is the segment after the last ``>>``.
    if ">>" in value:
        outcome = value.split(">>")[-1].strip()
        verdict = _OUTCOME_MAP.get(_norm_outcome(outcome))
        return ParsedCaseStatus(
            lifecycle_status=DECIDED,
            verdict_type=verdict or OTHER,
            verdict_outcome_raw=outcome or None,
            verdict_date_bs=date_bs,
            verdict_date_ad=date_ad,
            unmapped=verdict is None,
        )

    # R4/R5 — decided marker (with or without a paren date), outcome not in the
    # status itself; leave verdict_type for the hearing-based resolver to fill.
    if date_bs or value.startswith(_DECIDED_MARKERS):
        return ParsedCaseStatus(
            lifecycle_status=DECIDED,
            verdict_date_bs=date_bs,
            verdict_date_ad=date_ad,
        )

    return ParsedCaseStatus(lifecycle_status=UNKNOWN)


def apply_status(target: dict, raw_value: str | None) -> None:
    """Fill a CourtCase update-dict's status columns from a raw status string.

    Integration point for the enrichment spiders: replaces the old
    ``target["case_status"] = value[:100]`` one-liners. Behaviour:

    - stores ``case_status`` only when the value is a *real* status — a header
      artifact / empty value (lifecycle UNKNOWN) is dropped, not stored (DQ-01);
    - sets ``verdict_type`` to the normalised enum when derivable (DQ-02);
    - fills ``verdict_date_bs``/``_ad`` from a paren-embedded date **only if the
      caller has not already set one** from a dedicated verdict-date field, so an
      authoritative field always wins (DQ-03).

    Mutates ``target`` in place; sets nothing it cannot derive.
    """
    parsed = parse_case_status(raw_value)
    if parsed.lifecycle_status != UNKNOWN:
        target["case_status"] = normalize_whitespace(raw_value or "")[:100]
    if parsed.verdict_type:
        target["verdict_type"] = parsed.verdict_type
    if parsed.verdict_date_bs and not target.get("verdict_date_bs"):
        target["verdict_date_bs"] = parsed.verdict_date_bs
        target["verdict_date_ad"] = parsed.verdict_date_ad


def verdict_from_hearings(hearings) -> str | None:
    """Best-effort verdict_type from the final decisive hearing.

    ``hearings`` is ``extra_data.enrichment_hearings`` (list of dicts with
    ``case_status`` and ``decision_type``). Used to fill an outcome the
    case_status string never carried (esp. Special/Supreme paren-date rows).
    Returns None when no hearing carries a terminal decision.
    """
    if not hearings:
        return None
    for hearing in reversed(hearings):
        # Two hearing key-schemas exist: Special uses case_status/decision_type,
        # Supreme uses status/order_type. Accept either.
        status = (hearing.get("case_status") or hearing.get("status") or "").strip()
        if status != "फैसला":
            continue
        decision = normalize_whitespace(
            hearing.get("decision_type") or hearing.get("order_type") or ""
        )
        for key, verdict in _HEARING_DECISION_MAP.items():
            if key in decision:
                return verdict
    return None
