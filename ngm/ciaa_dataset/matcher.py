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
CONFIRMED_THRESHOLD = 0.85  # >= 0.85 auto-confirmed, no LLM (balanced approach)
UNDATED_CONFIRMED_THRESHOLD = 0.90  # Higher bar for undated PRs (no date validation)
NEEDS_REVIEW_THRESHOLD = 0.0  # Anything below CONFIRMED goes to LLM for verification

# Keywords that indicate a PR is a genuine criminal/corruption case
# If none of these appear in the PR title, it's not a court case PR
# NOTE: Old PRs (IDs ~1-730) have generic date-only titles — we skip keyword check for those
_CRIMINAL_KEYWORDS = [
    "आरोप-पत्र",
    "आरोपत्र",
    "आरोप पत्र",
    "आरोपपत्र",  # charge sheet (with/without hyphen)
    "भ्रष्टाचार",  # corruption
    "नियन्त्रण",  # arrested/controlled
    "गैरकानूनी",  # illegal
    "हानि",
    "हानी",  # loss/damage
    "घुस",  # bribe
    "अनियमितता",  # irregularity
    "दुरुपयोग",  # misuse
    "नक्कली",  # fake (fake certificate fraud cases)
    "हिरासत",  # custody/arrest
    "पक्राउ",  # arrested/caught
    "निलम्बन",  # suspended
]

# Phrases that indicate a PR is NOT a criminal case even if it contains criminal keywords
# e.g. "दुरुपयोग" appears in foundation day speeches about the CIAA itself
_NON_CRIMINAL_PHRASES = [
    "स्थापना दिवस",  # foundation/anniversary day
    "वार्षिक प्रतिवेदन",  # annual report
    "पदभार ग्रहण",  # taking office ceremony
    "विदाई",  # farewell
    "रक्तदान",  # blood donation
    "अन्तरक्रिया",  # interaction program
]

# Old PRs (IDs <= 730) have generic date-only titles; skip keyword check for them
_OLD_PR_THRESHOLD = 730

# Blocklist: PRs that should NEVER be matched (foundation day speeches, etc.)
_BLOCKED_PR_IDS = {
    2953,  # Foundation day speech (३४ औं स्थापना दिवस)
}

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

    # Remove parentheses and their content (e.g., "(पुन)" -> "पुन")
    text = text.replace("(", " ").replace(")", " ")

    # Character equivalences for common Nepali spelling variations
    # These characters are often used interchangeably or confused
    # IMPORTANT: Process multi-character sequences FIRST before single characters
    equivalences = [
        ("ङ्ग", "ंग"),
        ("न्स", "ंस"),  # अन्सारी vs अंसारी
        ("ण्ड", "ंड"),  # मण्डल vs मंडल (ण्ड → ंड)
        ("ज़", "ज"),
        ("ं", ""),  # Remove anusvara to handle गौस vs गौंस
        ("ँ", ""),  # Remove chandrabindu
        ("ी", "ि"),  # Long vs short i vowel: हीम vs हिम
        ("ू", "ु"),  # Long vs short u vowel
        ("े", ""),  # Remove e vowel sign for better matching
        ("ै", ""),  # Remove ai vowel sign
        ("ो", ""),  # Remove o vowel sign
        ("ौ", ""),  # Remove au vowel sign
        ("व", "ब"),
        ("ब", "व"),
        ("ष", "श"),
        ("श", "ष"),
        ("ङ", "न"),
        ("ण", "न"),  # मण्डल vs मंडल, णि vs नि
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
    Check if press release is within ±3 days of case registration.

    Press releases are typically published on or shortly after case filing.
    In rare cases, they may be published up to 3 days before registration.

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
    return -3 <= diff <= 3


def _calculate_day_difference(case_date: str, pr_date: str) -> Optional[int]:
    """Calculate absolute day difference between two BS dates."""
    if not case_date or not pr_date:
        return None

    def to_approx_days(date_str: str) -> Optional[int]:
        parts = date_str.split("-")
        if len(parts) != 3:
            return None
        try:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            return y * 365 + m * 30 + d
        except ValueError:
            return None

    case_days = to_approx_days(case_date)
    pr_days = to_approx_days(pr_date)

    if case_days is None or pr_days is None:
        return None

    return abs(case_days - pr_days)


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
            # to catch PRs within ±3 days that cross month boundaries
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

        logger.debug(
            f"Found {len(candidates)} candidate PRs for date {case_date_bs} (buckets: {buckets})"
        )
        return candidates

    def _is_criminal_case_pr(self, pr: dict) -> bool:
        """
        Check if a PR is about a genuine criminal/corruption case.
        Rejects PRs like annual reports, interaction programs, foundation day speeches etc.
        Old PRs (ID <= 730) have generic date-only titles — skip keyword check for those.
        """
        press_id = int(pr.get("press_id") or 0)

        # REJECT: Blocklisted PRs (foundation day speeches, etc.)
        if press_id in _BLOCKED_PR_IDS:
            logger.debug(f"PR #{press_id} rejected: in blocklist")
            return False

        if press_id <= _OLD_PR_THRESHOLD:
            return True  # Can't tell from title alone, allow through
        pr_title = pr.get("title") or ""
        # Reject if any non-criminal phrase is present (overrides criminal keywords)
        if any(phrase in pr_title for phrase in _NON_CRIMINAL_PHRASES):
            logger.debug(f"PR #{press_id} rejected: non-criminal phrase")
            return False
        has_keywords = any(keyword in pr_title for keyword in _CRIMINAL_KEYWORDS)
        if not has_keywords:
            logger.debug(f"PR #{press_id} rejected: no criminal keywords")
        return has_keywords

    def _is_surname_only_match(self, defendant_name: str) -> bool:
        """
        Check if a defendant name is likely just a surname (single word).
        Returns True if it's a single word, False if it has multiple words (first + last name).
        """
        name_parts = defendant_name.strip().split()
        return len(name_parts) == 1

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

        # REJECT: PR is not a criminal/corruption case (annual reports, programs, etc.)
        if not self._is_criminal_case_pr(pr):
            return 0.0, []

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

        pr_text_len = len(pr_text)
        use_partial = pr_text_len > 100

        best_sim = 0.0
        best_defendant = None
        best_matched_text = None

        for defendant_name in defendants_to_try:
            defendant_norm = normalize_nepali(defendant_name)
            name_parts = defendant_name.strip().split()

            # REJECT: Very short names (< 4 chars)
            if len(defendant_norm) < 4:
                continue

            # REJECT: Single-word surname-only unless long/unique (>= 8 chars)
            if self._is_surname_only_match(defendant_name) and len(defendant_norm) < 8:
                continue

            if not (defendant_norm and pr_text):
                continue

            if use_partial:
                sim = fuzz.partial_ratio(defendant_norm, pr_text) / 100.0

                # VALIDATE: For multi-word names, require first word (first name) also present in PR
                if sim > 0 and len(name_parts) >= 2:
                    first_name_norm = normalize_nepali(name_parts[0])
                    if len(first_name_norm) >= 3:
                        first_name_in_pr = (
                            fuzz.partial_ratio(first_name_norm, pr_text) / 100.0
                        )
                        if first_name_in_pr < 0.7:
                            # First name not found in PR — likely a surname-only match, penalize heavily
                            sim *= 0.4

                    # VALIDATE: Surname (last word) must also appear in PR
                    surname_norm = normalize_nepali(name_parts[-1])
                    if len(surname_norm) >= 3:
                        surname_in_pr = (
                            fuzz.partial_ratio(surname_norm, pr_text) / 100.0
                        )
                        if surname_in_pr < 0.7:
                            # Surname not found — penalize heavily
                            sim *= 0.4

            else:
                sim = fuzz.token_set_ratio(defendant_norm, pr_text) / 100.0

            # PENALIZE: Surname-only matches
            if self._is_surname_only_match(defendant_name):
                sim *= 0.7

            if sim > best_sim:
                best_sim = sim
                best_defendant = defendant_name
                # Extract matched text AFTER we know this is the best match
                if use_partial:
                    # Find the actual matched substring in original PR text
                    pr_combined_original = pr_title + (
                        " " + pr_full_text if pr_full_text else ""
                    )
                    # Use a larger window and finer step for better extraction
                    window = max(len(defendant_name) * 3, 50)
                    step = max(5, len(defendant_name) // 2)
                    from rapidfuzz import process

                    result = process.extractOne(
                        defendant_name,
                        [
                            pr_combined_original[i : i + window]
                            for i in range(
                                0, max(1, len(pr_combined_original) - window + 1), step
                            )
                        ],
                        scorer=fuzz.partial_ratio,
                    )
                    if result:
                        best_matched_text = result[0].strip()
                else:
                    best_matched_text = pr_title

        # REJECT: matched text too short (< 20 chars) — likely a surname fragment
        if best_matched_text and len(best_matched_text.strip()) < 20:
            logger.debug(
                f"PR #{pr.get('press_id')}: Rejected - matched text too short ({len(best_matched_text.strip())} chars): '{best_matched_text.strip()}'"
            )
            return 0.0, []

        if best_sim >= 0.50:
            score = best_sim
            signals.append(f"defendant_name_similarity({best_sim:.2f})")
            if best_defendant:
                signals.append(f"matched_defendant:{best_defendant}")
            if best_matched_text:
                signals.append(f"pr_matched_text:{best_matched_text}")
            logger.debug(
                f"PR #{pr.get('press_id')}: Score {best_sim:.2f}, defendant: {best_defendant}"
            )

        return score, signals

    def _match_press_releases(
        self,
        case: CourtCase,
        all_defendants: Optional[list[dict]] = None,
        defer_llm: bool = False,
    ) -> tuple[list[PressReleaseRecord], float, list[str], Optional[dict]]:
        """
        Score ALL candidate groups (same_day, ±1, ±2, undated), pick the highest
        score overall. Date proximity is used as a tiebreaker signal only.
        Undated PRs require a higher threshold (UNDATED_CONFIRMED_THRESHOLD) since there's no date validation.
        """
        candidates = self._candidates(case.registration_date_bs or "")
        case_date = case.registration_date_bs or ""

        # Bucket candidates by date proximity
        same_day, one_day, two_day, three_day, undated = [], [], [], [], []
        for pr in candidates:
            pr_date = pr.get("publication_date") or ""
            if not pr_date:
                undated.append(pr)
                continue
            day_diff = _calculate_day_difference(case_date, pr_date)
            if day_diff is None:
                undated.append(pr)
            elif day_diff == 0:
                same_day.append(pr)
            elif day_diff == 1:
                one_day.append(pr)
            elif day_diff == 2:
                two_day.append(pr)
            elif day_diff == 3:
                three_day.append(pr)

        candidate_groups = [
            (same_day, "same_day"),
            (one_day, "±1_day"),
            (two_day, "±2_days"),
            (three_day, "±3_days"),
            (undated, "undated"),
        ]

        # Score ALL groups and collect every match
        all_scored: list[tuple[float, dict, list[str], str]] = (
            []
        )  # (score, pr, signals, group)
        for group, group_label in candidate_groups:
            for pr in group:
                score, signals = self._score_press_release(case, pr, all_defendants)
                if score > 0:
                    all_scored.append(
                        (
                            score,
                            pr,
                            signals + [f"date_match:{group_label}"],
                            group_label,
                        )
                    )

        if not all_scored:
            return [], 0.0, [], None

        # Sort by score descending, pick the best overall
        all_scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_pr, best_signals, best_group = all_scored[0]

        # Apply group-specific minimum thresholds
        min_threshold = (
            UNDATED_CONFIRMED_THRESHOLD
            if best_group == "undated"
            else CONFIRMED_THRESHOLD
        )
        if best_score < min_threshold:
            # Below threshold — route to LLM if requested
            if defer_llm and all_defendants:
                top_candidates = [pr for _, pr, _, _ in all_scored[:5]]
                all_defendant_names = [
                    d["name"] for d in all_defendants if d.get("name")
                ]
                llm_defer_data = {
                    "defendant_names": all_defendant_names,
                    "press_release_candidates": top_candidates,
                    "scored_candidates": [(s, p, sig) for s, p, sig, _ in all_scored],
                }
                return [], best_score, best_signals, llm_defer_data
            return [], 0.0, [], None

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
        elif confidence > NEEDS_REVIEW_THRESHOLD:
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
