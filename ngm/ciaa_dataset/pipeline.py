"""CIAA Cases Dataset Pipeline - links Special Court cases with CIAA press releases and AG charge sheets."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from ngm.logging import setup  # noqa: E402

setup()
logger = logging.getLogger(__name__)


def _current_fiscal_year() -> int:
    """Return the current BS fiscal year based on today's date (approximate)."""
    # Nepal FY starts mid-July (Shrawan). Use Nepal timezone for accuracy near the boundary.
    now = datetime.now(ZoneInfo("Asia/Kathmandu"))
    return now.year + (57 if (now.month, now.day) >= (7, 16) else 56)


def _format_fiscal_year(fiscal_year: int) -> str:
    """
    Format fiscal year as dash-separated form.

    Args:
        fiscal_year: BS fiscal year integer (e.g. 2080)

    Returns:
        Formatted string (e.g. "2080-81")

    Example:
        2080 -> "2080-81"
        2059 -> "2059-60"
    """
    year_str = str(fiscal_year)
    next_year_last_two = f"{(int(year_str[-2:]) + 1) % 100:02d}"
    return f"{fiscal_year}-{next_year_last_two}"


def run_fiscal_year(
    fiscal_year: int,
    ag_index_path: str,
    press_releases_csv_path: Optional[str] = None,
) -> dict:
    """
    Run the pipeline for a single fiscal year.
    Returns a stats dict.
    """
    from ngm.ciaa_dataset.loader import DataLoader
    from ngm.ciaa_dataset.matcher import MatchingEngine, CONFIRMED_THRESHOLD
    from ngm.ciaa_dataset.builder import CIAACaseBuilder
    from ngm.ciaa_dataset.writer import FileWriter
    from ngm.ciaa_dataset.models import PressReleaseRecord

    fy_str = _format_fiscal_year(fiscal_year)
    logger.info("=== Processing fiscal year %s ===", fy_str)

    # Provide default path if not specified
    press_releases_csv_path = (
        press_releases_csv_path or "ngm/ciaa_dataset/data/ciaa-press-releases.csv"
    )

    loader = DataLoader(
        ag_index_path=ag_index_path,
        press_releases_csv_path=press_releases_csv_path,
    )
    builder = CIAACaseBuilder()
    writer = FileWriter()

    # Load data
    court_cases = loader.load_special_court_cases(fiscal_year)
    logger.info("Loaded %d Special Court cases for FY %s", len(court_cases), fy_str)

    press_releases = loader.load_press_release_index()
    ag_index = loader.load_ag_index()
    punaravedan_index = loader.load_punaravedan_index()

    engine = MatchingEngine(press_releases=press_releases, ag_index=ag_index)

    stats = {
        "fiscal_year": fy_str,
        "total": len(court_cases),
        "matched": 0,
        "needs_review": 0,
        "unmatched": 0,
        "write_failures": 0,
        "written": 0,
    }

    written_cases = []

    # Phase 1: Fuzzy matching (collect cases needing LLM verification)
    cases_needing_llm = []
    preliminary_matches = {}  # case_number -> (case, match, appeal, defendants)

    total_cases = len(court_cases)
    logger.info("Phase 1: Processing %d cases...", total_cases)

    # Track appeal statistics
    appeals_from_db = 0
    appeals_from_csv = 0

    for idx, case in enumerate(court_cases, 1):
        # Log progress every 10 cases
        if idx % 10 == 0 or idx == total_cases:
            logger.info("  [%d/%d] cases processed", idx, total_cases)

        # Resolve appeal
        appeal = None
        appeal_info_from_csv = None
        if case.case_number in punaravedan_index:
            appeal_info_from_csv = punaravedan_index[case.case_number]
            supreme_case_no = appeal_info_from_csv["supreme_case_number"]

            # Try to load from database first
            appeal = loader.load_supreme_court_case(supreme_case_no)

            if appeal:
                appeals_from_db += 1
                logger.debug(
                    "[%s] Found appeal in DB: %s",
                    case.case_number,
                    supreme_case_no,
                )
            else:
                # DB case doesn't exist, but we have CSV info
                appeals_from_csv += 1
                logger.debug(
                    "[%s] Appeal filed (CSV only): %s",
                    case.case_number,
                    supreme_case_no,
                )
        elif case.original_case_number:
            appeal = loader.load_supreme_court_case(case.original_case_number)

        # Load defendants
        defendants = loader.load_defendants(case.case_number, case.court_identifier)

        # Load plaintiffs
        plaintiffs = loader.load_plaintiffs(case.case_number, case.court_identifier)

        # Load appeal defendants and plaintiffs if appeal exists
        appeal_defendants = None
        appeal_plaintiffs = None
        if appeal:
            appeal_defendants = loader.load_defendants(
                appeal.case_number, appeal.court_identifier
            )
            appeal_plaintiffs = loader.load_plaintiffs(
                appeal.case_number, appeal.court_identifier
            )

        # Match with defer_llm=True to collect LLM verification data
        match = engine.match(case, all_defendants=defendants, defer_llm=True)

        preliminary_matches[case.case_number] = (
            case,
            match,
            appeal,
            appeal_info_from_csv,
            defendants,
            plaintiffs,
            appeal_defendants,
            appeal_plaintiffs,
        )

        # Check if LLM verification is needed
        if hasattr(match, "llm_defer_data") and match.llm_defer_data:
            defer_data = match.llm_defer_data
            if "confirmed_primary_pr" in defer_data:
                # Primary match is confirmed — check secondary PR candidates
                cases_needing_llm.append(
                    {
                        "case_number": case.case_number,
                        "defendant_names": defer_data["defendant_names"],
                        "confirmed_primary_pr": defer_data["confirmed_primary_pr"],
                        "secondary_pr_candidates": defer_data[
                            "secondary_pr_candidates"
                        ],
                    }
                )
            else:
                # Primary match needs LLM verification
                cases_needing_llm.append(
                    {
                        "case_number": case.case_number,
                        "defendant_names": defer_data["defendant_names"],
                        "press_release_candidates": defer_data[
                            "press_release_candidates"
                        ],
                        "scored_candidates": defer_data["scored_candidates"],
                    }
                )

    # Log Phase 1 summary
    logger.info(
        "Phase 1 complete: Appeals resolved - %d from DB, %d from CSV",
        appeals_from_db,
        appeals_from_csv,
    )

    # Phase 2: Batch LLM verification (ONE call for all cases)
    llm_results = {}
    llm_secondary_results: dict[str, list[int]] = {}

    # Separate cases needing primary match from those needing secondary PR check
    cases_needing_secondary: list[dict] = []
    for case_data in cases_needing_llm:
        if "confirmed_primary_pr" in case_data:
            cases_needing_secondary.append(case_data)
    cases_needing_primary = [
        c for c in cases_needing_llm if "confirmed_primary_pr" not in c
    ]

    if cases_needing_primary:
        logger.info(
            "Phase 2: Running batch LLM verification for %d cases",
            len(cases_needing_primary),
        )
        try:
            from ngm.ciaa_dataset.llm_verifier import LLMVerifier

            verifier = LLMVerifier()
            llm_results = verifier.verify_multi_case_batch(cases_needing_primary)
            logger.info(
                "Phase 2 complete: %d LLM verifications completed", len(llm_results)
            )
        except ValueError as e:
            # LLM misconfiguration (missing env vars, invalid model)
            logger.error(
                "LLM configuration error for %d cases; continuing without LLM verification: %s",
                len(cases_needing_primary),
                e,
            )
        except Exception as e:
            logger.error(
                "Batch LLM verification failed for %d cases; continuing without LLM signal: %s",
                len(cases_needing_primary),
                e,
            )

    if cases_needing_secondary:
        logger.info(
            "Phase 2b: Verifying secondary PRs for %d confirmed cases",
            len(cases_needing_secondary),
        )
        try:
            from ngm.ciaa_dataset.llm_verifier import LLMVerifier

            verifier = LLMVerifier()
            llm_secondary_results = verifier.verify_secondary_prs(
                cases_needing_secondary
            )
            logger.info(
                "Phase 2b complete: secondary PR check done for %d cases",
                len(llm_secondary_results),
            )
        except ValueError as e:
            logger.error(
                "LLM configuration error for secondary PR check; skipping: %s", e
            )
        except Exception as e:
            logger.error("Secondary PR LLM verification failed; skipping: %s", e)

    # Phase 3: Apply LLM results and build final cases
    logger.info("Phase 3: Building final case records...")
    for case_number, (
        case,
        match,
        appeal,
        appeal_info_from_csv,
        defendants,
        plaintiffs,
        appeal_defendants,
        appeal_plaintiffs,
    ) in preliminary_matches.items():
        # Apply LLM result if available
        if case_number in llm_results:
            matched_press_id, llm_confidence, explanation = llm_results[case_number]

            # Distinguish LLM error (transient) from intentional rejection.
            # Match all error prefixes emitted by the verifier regardless of case.
            expl_lower = (explanation or "").lower()
            is_llm_error = (
                expl_lower.startswith("llm_error:")
                or expl_lower.startswith("llm error:")
                or expl_lower.startswith("json parse error:")
            )

            if matched_press_id:
                # Find the matched PR from defer data
                defer_data = match.llm_defer_data
                scored_candidates = defer_data["scored_candidates"]

                matched_pr = None
                matched_signals = []
                for _score, pr, signals in scored_candidates:
                    try:
                        pr_id = int(pr.get("press_id", 0))
                    except (TypeError, ValueError):
                        continue
                    if pr_id == matched_press_id:
                        matched_pr = pr
                        matched_signals = signals
                        break

                if matched_pr:
                    # Update match with LLM result
                    match.press_releases = [
                        PressReleaseRecord(
                            release_id=matched_press_id,
                            url=matched_pr.get("source_url") or "",
                            r2_metadata_url=None,
                            date=matched_pr.get("publication_date") or "",
                            title=matched_pr.get("title") or "",
                        )
                    ]
                    match.confidence = max(match.confidence, llm_confidence)
                    # Preserve existing signals (e.g., ag_index_exact_match)
                    preserved_signals = [
                        s for s in match.match_signals if s.startswith("ag_index_")
                    ]
                    match.match_signals = [
                        *preserved_signals,
                        *matched_signals,
                        f"llm_multi_case_batch({llm_confidence:.2f})",
                    ]
                    match.match_status = (
                        "confirmed"
                        if match.confidence >= CONFIRMED_THRESHOLD
                        else "needs_review"
                    )
                    match.unmatched_reason = None

                    logger.info(
                        "[%s] LLM multi-case batch verified PR %s: %s",
                        case_number,
                        matched_press_id,
                        explanation,
                    )
            else:
                if is_llm_error:
                    # Transient error — keep fuzzy match result as needs_review for human review
                    match.match_status = "needs_review"
                    match.match_signals.append("llm_unavailable")
                    logger.warning(
                        "[%s] LLM unavailable, keeping fuzzy match as needs_review: %s",
                        case_number,
                        explanation,
                    )
                else:
                    # LLM intentionally rejected all candidates — downgrade to unmatched
                    match.press_releases = []
                    match.match_status = "unmatched"
                    match.unmatched_reason = "LLM rejected all candidates"
                    # Strip misleading signals from unmatched cases
                    match.match_signals = [
                        s
                        for s in match.match_signals
                        if not s.startswith("pr_matched_text:")
                        and not s.startswith("date_match:")
                        and not s.startswith("defendant_name_similarity")
                        and not s.startswith("matched_defendant:")
                    ]
                    match.match_signals.append("llm_multi_case_batch_rejected")
                    logger.info(
                        "[%s] LLM multi-case batch rejected all candidates: %s",
                        case_number,
                        explanation,
                    )

        # Apply secondary PR results (additional PRs for already-confirmed cases)
        if case_number in llm_secondary_results:
            additional_ids = llm_secondary_results[case_number]
            if additional_ids and match.press_releases:
                defer_data = match.llm_defer_data or {}
                secondary_candidates = defer_data.get("secondary_pr_candidates", [])
                existing_ids = {pr.release_id for pr in match.press_releases}
                for pr_id in additional_ids:
                    if pr_id in existing_ids:
                        continue
                    pr_dict = next(
                        (
                            p
                            for p in secondary_candidates
                            if int(p.get("press_id") or 0) == pr_id
                        ),
                        None,
                    )
                    if pr_dict:
                        match.press_releases.append(
                            PressReleaseRecord(
                                release_id=pr_id,
                                url=pr_dict.get("source_url") or "",
                                r2_metadata_url=None,
                                date=pr_dict.get("publication_date") or "",
                                title=pr_dict.get("title") or "",
                            )
                        )
                        match.match_signals.append(
                            f"llm_secondary_pr_confirmed:{pr_id}"
                        )
                        existing_ids.add(pr_id)

        # Safety check: confirmed/needs_review with no press releases is invalid
        if (
            match.match_status in ("confirmed", "needs_review")
            and not match.press_releases
            and not match.charge_sheets
        ):
            match.match_status = "unmatched"
            match.unmatched_reason = "No press release or charge sheet populated"
            match.match_signals = [
                s
                for s in match.match_signals
                if not s.startswith("pr_matched_text:")
                and not s.startswith("date_match:")
                and not s.startswith("defendant_name_similarity")
                and not s.startswith("matched_defendant:")
            ]

        # Track stats
        if match.match_status == "confirmed":
            stats["matched"] += 1
        elif match.match_status == "needs_review":
            stats["needs_review"] += 1
        else:
            stats["unmatched"] += 1

        # Build
        ciaa_case = builder.build(
            case=case,
            match=match,
            fiscal_year=fiscal_year,
            appeal=appeal,
            appeal_info_from_csv=appeal_info_from_csv,
            defendants=defendants or None,
            plaintiffs=plaintiffs or None,
            appeal_defendants=appeal_defendants or None,
            appeal_plaintiffs=appeal_plaintiffs or None,
        )

        # Write
        try:
            writer.write_case(ciaa_case)
            written_cases.append(ciaa_case)
            stats["written"] += 1
        except Exception as e:
            logger.error("[%s] Failed to write case: %s", case.case_number, e)
            stats["write_failures"] += 1

    # Flush all pending writes in parallel
    try:
        writer.flush()
    except Exception as e:
        # Count buffered-but-unflushed cases as failures
        pending_count = len(writer._pending_writes)
        if pending_count > 0:
            stats["written"] -= pending_count
            stats["write_failures"] += pending_count
            logger.error("Failed to flush %d pending case writes: %s", pending_count, e)
        else:
            logger.error("Failed to flush case writes: %s", e)
        raise

    # Write FY index
    try:
        writer.write_fiscal_year_index(fy_str, written_cases, stats)
    except Exception as e:
        logger.error("Failed to write FY index for %s: %s", fy_str, e)
        stats["write_failures"] += 1
        raise

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="CIAA Cases Dataset Pipeline")
    parser.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="Process all BS fiscal years from YEAR up to the current year. Defaults to current year only.",
    )
    args = parser.parse_args()

    current_fy = _current_fiscal_year()

    if args.from_year is not None:
        if args.from_year <= 0:
            logger.error(
                "--from-year %d is invalid; expected a positive BS fiscal year",
                args.from_year,
            )
            sys.exit(1)
        if args.from_year > current_fy:
            logger.error(
                "--from-year %d is in the future (current FY is %d)",
                args.from_year,
                current_fy,
            )
            sys.exit(1)
        fiscal_years = list(range(args.from_year, current_fy + 1))
        logger.info(
            "Processing fiscal years: %d - %d (%d years)",
            args.from_year,
            current_fy,
            len(fiscal_years),
        )
    else:
        fiscal_years = [current_fy]

    all_stats = []
    failed_years = []
    for fy in fiscal_years:
        try:
            stats = run_fiscal_year(
                fiscal_year=fy,
                ag_index_path="ngm/ciaa_dataset/data/ag_index.csv",
                press_releases_csv_path="ngm/ciaa_dataset/data/ciaa-press-releases.csv",
            )
            all_stats.append(stats)
            if stats.get("write_failures", 0) > 0:
                logger.error(
                    "Fiscal year %d completed with %d write failures",
                    fy,
                    stats["write_failures"],
                )
                failed_years.append(fy)
        except Exception:
            logger.exception("Pipeline failed for fiscal year %d", fy)
            failed_years.append(fy)
            # Continue processing remaining years rather than aborting
            if len(fiscal_years) == 1:
                sys.exit(1)

    # Summary report
    logger.info("\n=== CIAA Dataset Pipeline Summary ===")
    for s in all_stats:
        logger.info(
            "  FY %s: total=%d matched=%d needs_review=%d unmatched=%d write_failures=%d written=%d",
            s["fiscal_year"],
            s["total"],
            s["matched"],
            s["needs_review"],
            s["unmatched"],
            s.get("write_failures", 0),
            s["written"],
        )
    if failed_years:
        logger.error("  FAILED years: %s", ", ".join(str(y) for y in failed_years))
    logger.info("=====================================\n")

    if failed_years:
        sys.exit(1)


if __name__ == "__main__":
    main()
