import scrapy
import re
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from ngm.database.models import get_engine, BlacklistedFirm
from ngm.utils.db_helpers import convert_bs_to_ad

from ngm.ngscrape.items import BlacklistedFirmItem

# PPMO was established in BS 2064. Anything outside this band is not a BS date
# from a real blacklist row (the source page sometimes mixes AD-formatted text
# into table cells, which historically caused garbage rows to be persisted).
_VALID_BS_YEAR = (2060, 2099)
_BS_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _cell_text(cell) -> str:
    """Join all text nodes in a cell and collapse whitespace (incl. NBSP)."""
    parts = cell.xpath(".//text()").getall()
    joined = " ".join(parts).replace(" ", " ")
    return re.sub(r"\s+", " ", joined).strip()


def _looks_like_firm_name(s: str) -> bool:
    """Reject pagination arrows, headers, and other non-firm strings."""
    if not s or len(s) < 3:
        return False
    if s in {"»", "›", ">", "<", "«", "‹"}:
        return False
    if "विवरण" in s or "company name" in s.lower():
        return False
    # Require at least 2 letters (Devanagari or Latin) — pure-punctuation rows fail.
    letters = sum(1 for ch in s if ch.isalpha())
    return letters >= 2


def _parse_bs_year(date_str: str) -> int | None:
    m = _BS_DATE_RE.match(date_str.strip()) if date_str else None
    return int(m.group(1)) if m else None


class PPMOBlacklistSpider(scrapy.Spider):
    name = "ppmo_blacklist"
    # Using old. subdomain because main site returns 404 for blacklist
    start_urls = ["https://old.ppmo.gov.np/index.php?route=information/black_lists"]

    custom_settings = {
        "DOWNLOADER_CLIENT_TLS_VERIFY": False,
        "ROBOTSTXT_OBEY": False,  # Avoid issues with robots.txt on old subdomain
        "ITEM_PIPELINES": {
            "ngm.ngscrape.pipelines.PPMOBlacklistPipeline": 300,
        },
    }

    def __init__(self, *args, **kwargs):
        super(PPMOBlacklistSpider, self).__init__(*args, **kwargs)
        self.engine = get_engine()
        self.Session = sessionmaker(bind=self.engine)

    def parse(self, response):
        # The table has class 'list4' on the old site
        rows = response.xpath("//table[@class='list4']//tr")

        if not rows:
            # Fallback to generic table rows if list4 not found
            rows = response.xpath("//table//tr[td]")

        if not rows:
            self.logger.warning(
                "No rows found in PPMO blacklist table. Check selectors."
            )
            return

        for row in rows:
            cols = row.xpath("td")
            if len(cols) < 2:
                continue

            firm_name = _cell_text(cols[0])
            detail_url = cols[0].xpath(".//a/@href").get()

            if not _looks_like_firm_name(firm_name):
                self.logger.debug(f"Skipping non-firm row: {firm_name!r}")
                continue

            duration_text = _cell_text(cols[1])

            item = BlacklistedFirmItem(
                firm_name=firm_name,
                duration=duration_text,
                source_url=response.url,
            )

            if detail_url:
                yield response.follow(
                    detail_url, self.parse_detail, cb_kwargs={"item": item}
                )
            else:
                if not self.process_dates(item):
                    continue
                self.save_item(item)
                yield item

        # Handle pagination
        next_page = response.xpath(
            "//div[@class='pagination']//a[contains(text(), '>')]/@href"
        ).get()
        if next_page:
            yield response.follow(next_page, self.parse)

    def parse_detail(self, response, item):
        # The detail page uses a table with class 'list3'
        rows = response.xpath("//table[@class='list3']//tr")

        if not rows:
            # We followed a link that wasn't actually a detail page (e.g. pagination).
            # Don't persist a half-empty row; just bail out.
            self.logger.warning(
                f"No detail rows on {response.url} for firm={item.get('firm_name')!r}; skipping"
            )
            return

        for row in rows:
            label = row.xpath("td[1]//text()").get("").strip()
            # Join all text pieces in value to handle multi-line/nested content
            value = " ".join(row.xpath("td[2]//text()").getall()).strip()

            if "Address" in label:
                item["address"] = value
            elif "Cause" in label:
                item["reason"] = value

                # Extract proprietor name from Cause text if available
                # Pattern: (मुख्य व्यक्ति: श्री मित्रलाल सापकोटा)
                prop_match = re.search(r"मुख्य व्यक्ति:\s*([^)]+)", value)
                if prop_match:
                    item["proprietor_name"] = (
                        prop_match.group(1).replace("श्री", "").strip()
                    )

                # Extract recommending office
                # Pattern: ( कालो सूचीमा राख्न लेखि पठाउने सार्बजनिक निकायको नाम :श्री सडक डिभिजन, इलाम)
                office_match = re.search(r"सार्बजनिक निकायको नाम\s*:?\s*([^)]+)", value)
                if office_match:
                    item["recommending_office"] = (
                        office_match.group(1).replace("श्री", "").strip()
                    )

        if not self.process_dates(item):
            return
        self.save_item(item)

        # Prepare for NES mapping
        item["entity_type"] = "organization"
        item["entity_sub_type"] = "contractor"
        item["names"] = [{"name": item["firm_name"], "kind": "PRIMARY"}]

        yield item

    def process_dates(self, item) -> bool:
        """Parse duration into BS/AD dates. Returns False if the duration field
        doesn't look like a real BS date range (e.g. AD-formatted values from
        non-firm rows that slipped past upstream filters)."""
        duration_text = item.get("duration", "") or ""

        if " to " in duration_text:
            blacklist_date_bs, _, effective_until_bs = duration_text.partition(" to ")
            blacklist_date_bs = blacklist_date_bs.strip()
            effective_until_bs = effective_until_bs.strip()
        else:
            blacklist_date_bs = duration_text.strip()
            effective_until_bs = None

        # Validate at least the first date looks like a plausible BS year. PPMO
        # was established BS 2064; typical blacklist dates are 2065+. AD-format
        # strings like "2017-09-04" historically slipped in and produced nonsense
        # AD dates after BS conversion (BS 2017 = AD 1960).
        year = _parse_bs_year(blacklist_date_bs)
        if year is None or not (_VALID_BS_YEAR[0] <= year <= _VALID_BS_YEAR[1]):
            self.logger.warning(
                f"Implausible BS date {blacklist_date_bs!r} for firm "
                f"{item.get('firm_name')!r}; skipping row"
            )
            return False

        item["blacklist_date_bs"] = blacklist_date_bs
        item["effective_until_bs"] = effective_until_bs

        try:
            item["blacklist_date_ad"] = convert_bs_to_ad(blacklist_date_bs)
            if effective_until_bs:
                item["effective_until_ad"] = convert_bs_to_ad(effective_until_bs)
        except Exception as e:
            self.logger.warning(
                f"BS→AD conversion failed for {blacklist_date_bs!r}: {e}"
            )
        return True

    def save_item(self, item):
        session = self.Session()
        try:
            # Check if already exists
            existing = (
                session.query(BlacklistedFirm)
                .filter_by(
                    firm_name=item["firm_name"],
                    blacklist_date_bs=item["blacklist_date_bs"],
                )
                .first()
            )

            if not existing:
                firm = BlacklistedFirm(
                    firm_name=item["firm_name"],
                    proprietor_name=item.get("proprietor_name"),
                    address=item.get("address"),
                    blacklist_date_bs=item["blacklist_date_bs"],
                    blacklist_date_ad=item.get("blacklist_date_ad"),
                    effective_until_bs=item.get("effective_until_bs"),
                    effective_until_ad=item.get("effective_until_ad"),
                    duration=item.get("duration"),
                    reason=item.get("reason"),
                    recommending_office=item.get("recommending_office"),
                    scraped_at=datetime.utcnow(),
                )
                session.add(firm)
                session.commit()
                self.logger.info(f"Added blacklisted firm: {item['firm_name']}")
            else:
                # Optionally update existing record if more data is now available
                updated = False
                if item.get("address") and not existing.address:
                    existing.address = item.get("address")
                    updated = True
                if item.get("proprietor_name") and not existing.proprietor_name:
                    existing.proprietor_name = item.get("proprietor_name")
                    updated = True
                if item.get("reason") and not existing.reason:
                    existing.reason = item.get("reason")
                    updated = True

                if updated:
                    session.commit()
                    self.logger.info(f"Updated blacklisted firm: {item['firm_name']}")

        except Exception as e:
            session.rollback()
            self.logger.error(f"Error saving {item['firm_name']}: {e}")
        finally:
            session.close()
