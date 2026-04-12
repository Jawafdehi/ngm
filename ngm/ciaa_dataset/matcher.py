"""MatchingEngine - links Special Court cases to CIAA press releases and AG charge sheets."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Optional

from rapidfuzz import fuzz

from ngm.database.models import CourtCase
from ngm.utils.normalizer import normalize_whitespace, nepali_to_roman_numerals
from ngm.ciaa_dataset.models import MatchResult, PressReleaseRecord, AbhiyogPatraRecord

logger = logging.getLogger(__name__)

# Confidence thresholds
CONFIRMED_THRESHOLD = 0.6
NEEDS_REVIEW_THRESHOLD = 0.2

# Honorifics to strip from Nepali names before comparison
_HONORIFICS = re.compile(
    r"\b(श्री|माननीय|डा\.|डा|प्रा\.|प्रा|अध्यक्ष|सदस्य|सचिव|महासचिव|उपसचिव|सहसचिव)\b"
)


def normalize_nepali(text: str) -> str:
    """
    Normalize Nepali text for fuzzy matching.

    Handles:
    - Honorific removal
    - Devanagari numeral conversion
    - Character equivalences (common spelling variations)
    - Whitespace normalization
    """
    if not text:
        return ""

    # Convert Devanagari numerals to Roman
    text = nepali_to_roman_numerals(text)

    # Remove honorifics
    text = _HONORIFICS.sub("", text)

    # Character equivalences for common Nepali spelling variations
    # These characters are often used interchangeably or confused
    # IMPORTANT: Process multi-character sequences FIRST before single characters
    equivalences = [
        ("ङ्ग", "ंग"),
        ("न्स", "ंस"),  # अन्सारी vs अंसारी
        ("ज़", "ज"),
        ("ं", "ँ"),
        ("ँ", "ं"),
        ("व", "ब"),
        ("ब", "व"),
        ("ष", "श"),
        ("श", "ष"),
        ("ङ", "ं"),
    ]

    for old, new in equivalences:
        text = text.replace(old, new)

    # Normalize whitespace
    text = normalize_whitespace(text)

    return text.lower()


def _parse_bs_month(date_bs: str) -> Optional[tuple[int, int]]:
    """Return (year, month) from a BS date string 'YYYY-MM-DD', or None."""
    if not date_bs:
        return None
    parts = date_bs.split("-")
    if len(parts) >= 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return None


def _is_within_date_range(case_date_bs: str, release_date: str) -> bool:
    """
    Check if press release is within ±2 days of case registration.

    Press releases are typically published on or shortly after case filing.
    In rare cases, they may be published up to 2 days before registration.

    Undated press releases (empty release_date) are always included.
    """

    def to_approx_days(date_str: str) -> Optional[int]:
        parts = date_str.split("-")
        if len(parts) != 3:
            return None
        try:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            return y * 365 + m * 30 + d
        except ValueError:
            return None

    # Undated press releases are always included
    if not release_date or not release_date.strip():
        return True

    case_days = to_approx_days(case_date_bs)
    release_days = to_approx_days(release_date)

    if case_days is None or release_days is None:
        return False

    diff = release_days - case_days
    return -2 <= diff <= 2


class MatchingEngine:
    """
    Links each Special Court case to its CIAA press releases and AG charge sheets.

    Call `build_index()` once after loading data, then `match()` per case.
    """

    def __init__(
        self,
        press_releases: list[dict],
        ag_index: dict[str, dict],
    ):
        self.ag_index = ag_index
        self._press_releases = press_releases
        # Month-bucket inverted index: (year, month) -> list of press release dicts
        self._pr_by_month: dict[tuple[int, int], list[dict]] = defaultdict(list)
        self._build_index()

    def _build_index(self) -> None:
        for pr in self._press_releases:
            date = pr.get("publication_date") or ""
            ym = _parse_bs_month(date)
            if ym:
                self._pr_by_month[ym].append(pr)
            else:
                # No date — put in a catch-all bucket so it's still considered
                self._pr_by_month[(-1, -1)].append(pr)
        logger.info(
            "Press release index built: %d records across %d month buckets",
            len(self._press_releases),
            len(self._pr_by_month),
        )

    def _candidates(self, case_date_bs: str) -> list[dict]:
        """Return press releases from the same month as case date, plus adjacent months and undated ones."""
        ym = _parse_bs_month(case_date_bs)
        buckets: set[tuple[int, int]] = {(-1, -1)}  # always include undated

        if ym:
            year, month = ym
            # Include same month, previous month, and next month
            # to catch PRs within ±2 days that cross month boundaries
            buckets.update(
                {
                    ym,  # Same month
                    (
                        (year - 1, 12) if month == 1 else (year, month - 1)
                    ),  # Previous month
                    (year + 1, 1) if month == 12 else (year, month + 1),  # Next month
                }
            )

        candidates = []
        for bucket in buckets:
            bucket_prs = self._pr_by_month.get(bucket, [])
            candidates.extend(bucket_prs)

        return candidates

    def _score_press_release(
        self, case: CourtCase, pr: dict, all_defendants: Optional[list[dict]] = None
    ) -> tuple[float, list[str]]:
        """
        Return (score, signals) for a single press release candidate.

        Press release matching: defendant name similarity only.
        Date filtering is done in _candidates() method.
        """
        score = 0.0
        signals: list[str] = []

        pr_title = pr.get("title") or ""
        pr_full_text = (pr.get("full_text") or "").strip()

        # Filter out meaningless full_text (just dashes, whitespace, etc.)
        if pr_full_text and not pr_full_text.replace("-", "").replace("_", "").strip():
            pr_full_text = ""

        # Combine title and full_text for better matching
        if pr_full_text and pr_title:
            pr_text = normalize_nepali(pr_title + " " + pr_full_text)
        else:
            pr_text = normalize_nepali(pr_full_text or pr_title)

        # Try all defendants if available, otherwise fall back to primary defendant from case text
        defendants_to_try = []
        if all_defendants:
            defendants_to_try = [d["name"] for d in all_defendants if d.get("name")]
        else:
            # Fallback: extract primary defendant from case.defendant text
            defendant_primary = (case.defendant or "").split("समेत")[0].strip()
            if defendant_primary:
                defendants_to_try = [defendant_primary]

        # Defendant name similarity (full score based on best match)
        # For long texts, use partial_ratio to find name within text
        # For very short texts (just a name), use token_set_ratio
        best_sim = 0.0
        best_defendant = None

        pr_text_len = len(pr_text)
        use_partial = (
            pr_text_len > 100
        )  # Use partial matching for texts longer than 100 chars

        for defendant_name in defendants_to_try:
            defendant_norm = normalize_nepali(defendant_name)
            if defendant_norm and pr_text:
                if use_partial:
                    # For longer texts, find the defendant name within the text
                    sim = fuzz.partial_ratio(defendant_norm, pr_text) / 100.0
                else:
                    # For very short texts, use token_set_ratio
                    sim = fuzz.token_set_ratio(defendant_norm, pr_text) / 100.0
                if sim > best_sim:
                    best_sim = sim
                    best_defendant = defendant_name

        if best_sim >= 0.50:  # Lowered threshold to catch spelling variations
            score = best_sim  # Full score based on name similarity
            signals.append(f"defendant_name_similarity({best_sim:.2f})")
            if best_defendant:
                signals.append(f"matched_defendant:{best_defendant}")

        return score, signals

    def _match_press_releases(
        self,
        case: CourtCase,
        all_defendants: Optional[list[dict]] = None,
        defer_llm: bool = False,
    ) -> tuple[list[PressReleaseRecord], float, list[str], Optional[dict]]:
        """Return the best-matching press release within ±2 days of case registration."""
        candidates = self._candidates(case.registration_date_bs or "")
        case_date = case.registration_date_bs or ""

        # Filter candidates by date range: ±2 days from registration
        valid_candidates = []
        for pr in candidates:
            pr_date = pr.get("publication_date") or ""
            if _is_within_date_range(case_date, pr_date):
                valid_candidates.append(pr)

        if not valid_candidates:
            return [], 0.0, [], None

        # Score all valid candidates
        scored_candidates = []
        for pr in valid_candidates:
            score, signals = self._score_press_release(case, pr, all_defendants)
            if score > 0:  # Only keep candidates with some score
                scored_candidates.append((score, pr, signals))

        # Sort by score descending
        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        if not scored_candidates:
            return [], 0.0, [], None

        best_score, best_pr, best_signals = scored_candidates[0]

        # LLM verification for non-high-confidence matches
        # Only defer if caller requested it and score is in review range
        if (
            defer_llm
            and all_defendants
            and NEEDS_REVIEW_THRESHOLD <= best_score < CONFIRMED_THRESHOLD
        ):
            # Get top 5 candidates for batch verification
            top_candidates = [pr for _, pr, _ in scored_candidates[:5]]

            # Extract all defendant names
            all_defendant_names = [d["name"] for d in all_defendants if d.get("name")]

            # Return data for batch processing
            llm_defer_data = {
                "defendant_names": all_defendant_names,
                "press_release_candidates": top_candidates,
                "scored_candidates": scored_candidates,
            }
            return [], best_score, best_signals, llm_defer_data

        # High confidence (>= 0.7): return best match
        if best_score >= 0.7:
            matched = [
                PressReleaseRecord(
                    release_id=int(best_pr.get("press_id") or 0),
                    url=best_pr.get("source_url") or "",
                    r2_metadata_url=None,
                    date=best_pr.get("publication_date") or "",
                    title=best_pr.get("title") or "",
                )
            ]
            return matched, best_score, best_signals, None
        elif best_pr and best_score >= NEEDS_REVIEW_THRESHOLD:
            # Return only best match for medium confidence
            matched = [
                PressReleaseRecord(
                    release_id=int(best_pr.get("press_id") or 0),
                    url=best_pr.get("source_url") or "",
                    r2_metadata_url=None,
                    date=best_pr.get("publication_date") or "",
                    title=best_pr.get("title") or "",
                )
            ]
            return matched, best_score, best_signals, None

        return [], 0.0, [], None

    def _match_charge_sheet(
        self, case: CourtCase
    ) -> tuple[Optional[AbhiyogPatraRecord], float, list[str]]:
        """
        Return matched charge sheet (if any), confidence, and signals using exact case number
        """
        case_no = case.case_number or ""

        row = self.ag_index.get(case_no)
        if row:
            return (
                AbhiyogPatraRecord(
                    case_number=row["case_number"],
                    title=row["title"],
                    filing_date=row["filing_date"] or None,
                    pdf_url=row["pdf_url"] or None,
                    court_office=row["court_office"] or None,
                ),
                1.0,
                ["ag_index_exact_match"],
            )

        # case number doesn't match.
        return None, 0.0, []

    def match(
        self,
        case: CourtCase,
        all_defendants: Optional[list[dict]] = None,
        defer_llm: bool = False,
    ) -> MatchResult:
        """
        Match a Special Court case to press releases and charge sheets.

        Args:
            case: CourtCase object
            all_defendants: Optional list of all defendants from court_case_entities table.
                           If provided, will try matching all defendants against press releases.
            defer_llm: If True, return partial match with llm_defer_data for batch processing
        """
        press_releases, pr_score, pr_signals, llm_defer_data = (
            self._match_press_releases(case, all_defendants, defer_llm=defer_llm)
        )
        charge_sheet, ag_score, ag_signals = self._match_charge_sheet(case)

        charge_sheets = [charge_sheet] if charge_sheet else []
        all_signals = pr_signals + ag_signals

        # Overall confidence: max of the two scores (AG exact match is definitive)
        confidence = max(pr_score, ag_score)

        if confidence >= CONFIRMED_THRESHOLD:
            status = "confirmed"
            unmatched_reason = None
        elif confidence >= NEEDS_REVIEW_THRESHOLD:
            status = "needs_review"
            unmatched_reason = None
        else:
            status = "unmatched"
            unmatched_reason = (
                "No press release or charge sheet matched above threshold"
            )

        result = MatchResult(
            press_releases=press_releases,
            charge_sheets=charge_sheets,
            confidence=round(confidence, 4),
            match_signals=all_signals,
            match_status=status,
            unmatched_reason=unmatched_reason,
        )

        # Attach LLM defer data if present
        if llm_defer_data:
            result.llm_defer_data = llm_defer_data

        return result
