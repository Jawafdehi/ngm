"""CIAACaseBuilder - assembles CIAACase records from matched components."""

from __future__ import annotations

import re
from typing import Optional

from ngm.database.models import CourtCase
from ngm.ciaa_dataset.models import (
    CIAACase,
    CIAABlock,
    CaseMeta,
    CourtCaseRecord,
    Defendant,
)
from ngm.ciaa_dataset.matcher import MatchResult


def _court_order_urls(case: CourtCase) -> list[str]:
    """Extract court order file URLs from extra_data and prepend base URL."""
    if not case.extra_data:
        return []
    paths = case.extra_data.get("court_orders") or []
    base_url = "https://ngm-store.jawafdehi.org/uploads/"
    return [f"{base_url}{p}" for p in paths if p]


def _derive_title(match: MatchResult, case: CourtCase) -> str:
    """Use first matched press release title, fall back to defendant names."""
    if match.press_releases:
        title = match.press_releases[0].title
        if title:
            return title
    return (case.defendant or case.case_number or "").strip()


class CIAACaseBuilder:
    """Assembles CIAACase JSON records."""

    def build(
        self,
        case: CourtCase,
        match: MatchResult,
        fiscal_year: int,
        appeal: Optional[CourtCase] = None,
        defendants: Optional[list[dict]] = None,
    ) -> CIAACase:
        """
        Build a CIAACase from a CourtCase + MatchResult.

        Args:
            case: The Special Court CourtCase DB row.
            match: Output of MatchingEngine.match().
            fiscal_year: BS fiscal year integer (e.g. 2080).
            appeal: Optional Supreme Court CourtCase for the appeal.
            defendants: Optional structured defendant list from court_case_entities.
                        Falls back to parsing case.defendant text if not provided.
        """
        fy_str = str(fiscal_year)

        # Defendants from database; fallback to parsing case.defendant text
        if defendants:
            defendant_objs = [
                Defendant(name=d["name"]) for d in defendants if d.get("name")
            ]
        else:
            # Fallback: parse case.defendant text
            raw = (case.defendant or "").strip()
            # Split on common separators: comma, semicolon, pipe, slash
            # Remove "समेत" suffix if present
            raw = raw.split("समेत")[0].strip()
            parsed = [n.strip() for n in re.split(r"[,;|/]+", raw) if n.strip()]
            defendant_objs = [Defendant(name=n) for n in parsed]

        # Determine current_status
        status = case.case_status or ""
        is_faisala = "फैसला" in status

        court_case_record = CourtCaseRecord(
            court=case.court_identifier or "special",
            case_no=case.case_number,
            registration_date_bs=case.registration_date_bs,
            registration_date_ad=(
                str(case.registration_date_ad) if case.registration_date_ad else None
            ),
            defendants=defendant_objs,
            current_status="faisala" if is_faisala else "ongoing",
            faisala_link=_court_order_urls(case),
        )

        appeal_record = None
        if appeal:
            appeal_record = CourtCaseRecord(
                court=appeal.court_identifier or "supreme",
                case_no=appeal.case_number,
                registration_date_bs=appeal.registration_date_bs,
                registration_date_ad=(
                    str(appeal.registration_date_ad)
                    if appeal.registration_date_ad
                    else None
                ),
                defendants=[],
                current_status=(
                    "faisala" if "फैसला" in (appeal.case_status or "") else "ongoing"
                ),
                faisala_link=_court_order_urls(appeal),
            )

        meta = CaseMeta(
            match_confidence=match.confidence,
            match_signals=match.match_signals,
            unmatched_reason=match.unmatched_reason,
            match_status=match.match_status,
        )

        return CIAACase(
            case_no=case.case_number,
            case_title=_derive_title(match, case),
            fiscal_year=fy_str,
            jawafdehi_case_url=None,
            ciaa=CIAABlock(
                press_releases=match.press_releases,
                abhiyogPatras=match.charge_sheets,
            ),
            court_case=court_case_record,
            appealed_case=appeal_record,
            meta=meta,
        )
