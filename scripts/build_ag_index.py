"""
Build AG charge sheet index for fast lookups

Fetches Special Court charge sheets from ag.gov.np and saves to CSV.
The AG website organizes charge sheets by month using a sequential month_id
starting from BS 2078.
"""

import argparse
import csv
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Request timeout in seconds (connect, read)
DEFAULT_TIMEOUT = (10, 60)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def devanagari_to_ascii(text: str) -> str:
    """Convert Devanagari numerals (०-९) to ASCII (0-9)"""
    devanagari_digits = "०१२३४५६७८९"
    ascii_digits = "0123456789"
    trans = str.maketrans(devanagari_digits, ascii_digits)
    return text.translate(trans)


def calculate_month_id(year: int, month: int) -> int:
    """Calculate sequential month_id for AG website API

    2078: 1-12, 2079: 13-24, 2080: 25-36, etc.
    """
    if year < 2078:
        raise ValueError("AG month_id starts at BS 2078")
    if not 1 <= month <= 12:
        raise ValueError("month must be in 1..12")
    return (year - 2078) * 12 + month


def fetch_charge_sheets_for_month(year: int, month: int) -> List[Dict]:
    """Fetch Special Court charge sheets for a specific month

    Returns list of dicts with: case_number, title, filing_date, pdf_url, court_office
    """
    month_id = calculate_month_id(year, month)

    url = "https://ag.gov.np/abhiyogpatras"
    params = {"month_id": month_id, "code": "", "description": "undefined"}
    headers = {
        "Accept": "*/*",
        "Referer": "https://ag.gov.np/abhiyog",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        charge_sheets = []

        # Each charge sheet is in a div.post--item card
        for div in soup.find_all("div", class_="post--item"):
            try:
                # Extract metadata from the card header
                date_elem = div.find("ul", class_="nav meta")
                if not date_elem:
                    continue

                # Extract court/office info (before the || separator)
                meta_text = date_elem.get_text()
                court_office = (
                    meta_text.split("||")[0].strip() if "||" in meta_text else ""
                )

                # Extract title from h3 heading
                title_elem = div.find("h3", class_="h5")
                title = title_elem.get_text(strip=True) if title_elem else ""

                # Extract description (contains case number in Devanagari)
                desc_elem = div.find("div", class_="post--content")
                description = desc_elem.get_text(strip=True) if desc_elem else ""

                # Extract case number in Devanagari format (e.g., ०८१-CR-००५८)
                case_match = re.search(r"[०-९]{3}-CR-[०-९]{4}", description)
                if not case_match:
                    continue

                # Convert Devanagari to ASCII (e.g., 081-CR-0058)
                case_number = devanagari_to_ascii(case_match.group(0))

                # Extract filing date (after the || separator)
                filing_date = date_elem.get_text(strip=True).split("||")[-1].strip()

                # Extract PDF download URL
                pdf_link = div.find("a", href=True)
                if pdf_link:
                    href = pdf_link["href"].strip()
                    pdf_url = urljoin("https://ag.gov.np/", href)
                else:
                    pdf_url = None

                charge_sheets.append(
                    {
                        "case_number": case_number,
                        "title": title,
                        "filing_date": filing_date,
                        "pdf_url": pdf_url,
                        "court_office": court_office,
                    }
                )

            except (AttributeError, IndexError, TypeError, ValueError, KeyError) as e:
                logger.warning(f"  Failed to parse charge sheet card: {e}")
                continue
            except Exception:
                logger.exception(
                    "  Unexpected parser error while processing charge sheet card"
                )
                raise

        return charge_sheets

    except requests.exceptions.RequestException as e:
        logger.error(
            f"  Failed to fetch month {month_id} (year {year}, month {month}): {e}"
        )
        raise RuntimeError(f"Network error fetching month {month_id}: {e}") from e
    except Exception as e:
        logger.error(f"  Unexpected error fetching month {month_id}: {e}")
        raise RuntimeError(f"Unexpected error fetching month {month_id}: {e}") from e


def main() -> None:
    """Main entry point - fetches charge sheets and exports to CSV"""
    parser = argparse.ArgumentParser(
        description="Build AG charge sheet index from ag.gov.np",
        epilog="Example: python build_ag_index.py --output ag_index.csv --years 2078,2079,2080",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="ngm/ciaa_dataset/data/ag_index.csv",
        help="Output CSV file path (default: ngm/ciaa_dataset/data/ag_index.csv)",
    )
    parser.add_argument(
        "--years",
        default="2078,2079,2080,2081,2082,2083",
        help="Comma-separated BS years (default: 2078-2083)",
    )

    args = parser.parse_args()

    # Ensure output directory exists
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse year list
    try:
        years = [int(y.strip()) for y in args.years.split(",")]
    except ValueError as e:
        logger.error(f"Invalid year format: {e}")
        logger.error("Years must be comma-separated integers (e.g., 2078,2079,2080)")
        sys.exit(1)

    invalid_years = [year for year in years if year < 2078]
    if invalid_years:
        logger.error(
            "Unsupported BS year(s): %s. AG month_id starts at 2078.",
            invalid_years,
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Building AG charge sheet index")
    logger.info("=" * 60)
    logger.info(f"Years: {years}")
    logger.info(f"Output: {args.output}")
    logger.info("")

    all_sheets = {}

    # Fetch charge sheets for each month
    for year in years:
        logger.info(f"Year {year}:")
        for month in range(1, 13):
            sheets = fetch_charge_sheets_for_month(year, month)
            if sheets:
                logger.info(f"  Month {month:2d}: {len(sheets)} charge sheets")
                for sheet in sheets:
                    # Deduplication: prefer more complete records (with pdf_url) over incomplete ones
                    case_num = sheet["case_number"]
                    if case_num not in all_sheets:
                        all_sheets[case_num] = sheet
                    else:
                        existing = all_sheets[case_num]
                        # Replace if new record has pdf_url and existing doesn't
                        if sheet.get("pdf_url") and not existing.get("pdf_url"):
                            logger.debug(
                                f"  Replacing incomplete record for {case_num}"
                            )
                            all_sheets[case_num] = sheet
                        else:
                            logger.debug(f"  Skipping duplicate: {case_num}")

            # Add delay between requests to avoid overwhelming the server
            time.sleep(0.5)

    logger.info("")
    logger.info(f"Indexed {len(all_sheets)} unique charge sheets")

    # Export CSV (always write, even if empty, to avoid stale files)
    fieldnames = ["case_number", "title", "filing_date", "pdf_url", "court_office"]

    try:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            # Sort by case number for readability and write deduplicated records
            for sheet in sorted(all_sheets.values(), key=lambda x: x["case_number"]):
                writer.writerow(sheet)

        logger.info(
            "✓ Saved %d deduplicated records to %s",
            len(all_sheets),
            args.output,
        )
    except OSError as e:
        logger.error(f"Failed to write CSV file: {e}")
        sys.exit(1)

    logger.info("")
    logger.info("=" * 60)
    logger.info(f"Successfully indexed {len(all_sheets)} unique charge sheets")


if __name__ == "__main__":
    main()
