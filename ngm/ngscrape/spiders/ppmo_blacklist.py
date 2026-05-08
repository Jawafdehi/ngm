import scrapy
import re
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from ngm.database.models import get_engine, BlacklistedFirm
from ngm.utils.db_helpers import convert_bs_to_ad

from ngm.ngscrape.items import BlacklistedFirmItem


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
            # Extract basic text from td elements
            cols = row.xpath("td")

            if len(cols) < 2:
                continue

            # The first column usually contains the firm name and a link
            firm_name = cols[0].xpath(".//text()").get("").strip()
            detail_url = cols[0].xpath(".//a/@href").get()

            if (
                not firm_name
                or "विवरण" in firm_name
                or firm_name == "»"
                or "Company Name" in firm_name
            ):
                continue

            duration_text = cols[1].xpath(".//text()").get("").strip()
            firm_type = (
                cols[2].xpath(".//text()").get("").strip() if len(cols) > 2 else None
            )
            status = (
                cols[3].xpath(".//text()").get("").strip() if len(cols) > 3 else None
            )

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
                self.process_dates(item)
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

        self.process_dates(item)
        self.save_item(item)

        # Prepare for NES mapping
        item["entity_type"] = "organization"
        item["entity_sub_type"] = "contractor"
        item["names"] = [{"name": item["firm_name"], "kind": "PRIMARY"}]

        yield item

    def process_dates(self, item):
        """Parse duration and convert dates."""
        duration_text = item.get("duration", "")
        blacklist_date_bs = None
        effective_until_bs = None

        if " to " in duration_text:
            parts = duration_text.split(" to ")
            blacklist_date_bs = parts[0].strip()
            effective_until_bs = parts[1].strip()
        else:
            blacklist_date_bs = duration_text

        item["blacklist_date_bs"] = blacklist_date_bs
        item["effective_until_bs"] = effective_until_bs

        # Attempt to parse date and convert to AD
        try:
            if blacklist_date_bs:
                item["blacklist_date_ad"] = convert_bs_to_ad(blacklist_date_bs)
            if effective_until_bs:
                item["effective_until_ad"] = convert_bs_to_ad(effective_until_bs)
        except:
            pass

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
