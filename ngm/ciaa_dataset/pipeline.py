"""CIAA Cases Dataset Pipeline - links Special Court cases with CIAA press releases and AG charge sheets."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Earliest fiscal year with CIAA Special Court data
EARLIEST_FISCAL_YEAR = 2059


def _current_fiscal_year() -> int:
    """Return the current BS fiscal year based on today's date (approximate)."""
    # Nepal FY starts mid-July (Shrawan). Use a rough AD offset.
    now = datetime.now(timezone.utc)
    # BS year ≈ AD year + 56/57; FY starts ~July 16
    bs_year = now.year + 56
    if now.month < 7 or (now.month == 7 and now.day < 16):
        bs_year -= 1
    return bs_year


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

    fy_str = str(fiscal_year)
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

    for idx, case in enumerate(court_cases, 1):
        # Log progress every 10 cases
        if idx % 10 == 0 or idx == total_cases:
            logger.info("  [%d/%d] cases processed", idx, total_cases)

        # Resolve appeal
        appeal = None
        if case.case_number in punaravedan_index:
            appeal_info = punaravedan_index[case.case_number]
            supreme_case_no = appeal_info["supreme_case_number"]
            appeal = loader.load_supreme_court_case(supreme_case_no)
            if appeal:
                logger.info(
                    "[%s] Found appeal: %s",
                    case.case_number,
                    supreme_case_no,
                )
        elif case.original_case_number:
            appeal = loader.load_supreme_court_case(case.original_case_number)

        # Load defendants
        defendants = loader.load_defendants(case.case_number, case.court_identifier)

        # Match with defer_llm=True to collect LLM verification data
        match = engine.match(case, all_defendants=defendants, defer_llm=True)

        preliminary_matches[case.case_number] = (case, match, appeal, defendants)

        # Check if LLM verification is needed
        if hasattr(match, "llm_defer_data") and match.llm_defer_data:
            cases_needing_llm.append(
                {
                    "case_number": case.case_number,
                    "defendant_names": match.llm_defer_data["defendant_names"],
                    "press_release_candidates": match.llm_defer_data[
                        "press_release_candidates"
                    ],
                    "scored_candidates": match.llm_defer_data["scored_candidates"],
                }
            )

    # Phase 2: Batch LLM verification (ONE call for all cases)
    llm_results = {}
    if cases_needing_llm:
        logger.info(
            "Running batch LLM verification for %d cases", len(cases_needing_llm)
        )
        try:
            from ngm.ciaa_dataset.llm_verifier import LLMVerifier

            verifier = LLMVerifier()
            llm_results = verifier.verify_multi_case_batch(cases_needing_llm)
            logger.info(
                "Batch LLM verification completed: %d results", len(llm_results)
            )
        except Exception as e:
            logger.error("Batch LLM verification failed: %s", e)

    # Phase 3: Apply LLM results and build final cases
    for case_number, (case, match, appeal, defendants) in preliminary_matches.items():
        # Apply LLM result if available
        if case_number in llm_results:
            matched_press_id, llm_confidence, explanation = llm_results[case_number]

            if matched_press_id:
                # Find the matched PR from defer data
                defer_data = match.llm_defer_data
                scored_candidates = defer_data["scored_candidates"]

                matched_pr = None
                matched_signals = []
                for score, pr, signals in scored_candidates:
                    if int(pr.get("press_id", 0)) == matched_press_id:
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
                    match.match_signals = matched_signals + [
                        f"llm_multi_case_batch({llm_confidence:.2f})"
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
                # LLM rejected all candidates
                match.match_signals.append("llm_multi_case_batch_rejected")
                logger.info(
                    "[%s] LLM multi-case batch rejected all candidates: %s",
                    case_number,
                    explanation,
                )

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
            defendants=defendants or None,
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
        "--fiscal-year",
        type=int,
        default=None,
        help="BS fiscal year start (e.g. 2080). Defaults to current year.",
    )
    args = parser.parse_args()

    fiscal_year = args.fiscal_year or _current_fiscal_year()
    fiscal_years = [fiscal_year]

    all_stats = []
    for fy in fiscal_years:
        try:
            stats = run_fiscal_year(
                fiscal_year=fy,
                ag_index_path="ngm/ciaa_dataset/data/ag_index.csv",
                press_releases_csv_path="ngm/ciaa_dataset/data/ciaa-press-releases.csv",
            )
            all_stats.append(stats)
        except Exception as e:
            logger.error("Pipeline failed for fiscal year %d: %s", fy, e)
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
    logger.info("=====================================\n")


if __name__ == "__main__":
    main()
