import scrapy
from datetime import datetime
from ngm.database.models import get_engine, get_session, BlacklistedFirm
from ngm.utils.db_helpers import convert_bs_to_ad

class PPMOBlacklistSpider(scrapy.Spider):
    name = "ppmo_blacklist"
    start_urls = ["https://ppmo.gov.np/index.php?route=information/black_list"]

    def parse(self, response):
        # Try to find the table rows
        rows = response.xpath("//table//tr[td]")
        
        if not rows:
            self.logger.warning("No rows found in PPMO blacklist table. Check selectors.")
            return

        engine = get_engine()
        session = get_session(engine)

        with session.begin():
            for row in rows:
                cols = row.xpath("td//text()").getall()
                cols = [c.strip() for c in cols if c.strip()]
                
                if len(cols) < 5:
                    continue
                
                # Typical columns: S.N, Firm Name, Proprietor, Date, Duration, Recommending Office
                firm_name = cols[1]
                proprietor = cols[2]
                blacklist_date_bs = cols[3]
                
                # Attempt to parse date and convert to AD
                blacklist_date_ad = None
                try:
                    blacklist_date_ad = convert_bs_to_ad(blacklist_date_bs)
                except:
                    pass
                
                # Check if already exists
                existing = session.query(BlacklistedFirm).filter_by(
                    firm_name=firm_name, 
                    blacklist_date_bs=blacklist_date_bs
                ).first()
                
                if not existing:
                    firm = BlacklistedFirm(
                        firm_name=firm_name,
                        proprietor_name=proprietor,
                        blacklist_date_bs=blacklist_date_bs,
                        blacklist_date_ad=blacklist_date_ad,
                        scraped_at=datetime.utcnow()
                    )
                    session.add(firm)
                    self.logger.info(f"Added blacklisted firm: {firm_name}")

        session.close()

        # Handle pagination
        next_page = response.xpath("//ul[@class='pagination']//li/a[contains(text(), '>')]/@href").get()
        if next_page:
            yield response.follow(next_page, self.parse)

