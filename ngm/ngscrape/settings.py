# Scrapy settings for ngscrape project
import logging
import os
from dotenv import load_dotenv

load_dotenv()

FILES_STORE = os.getenv("FILES_STORE", "output")
logging.getLogger("protego._protego").setLevel(logging.INFO)
BOT_NAME = "ngscrape"
TIMEZONE = "Asia/Kathmandu"
LOG_LEVEL = "INFO"

SPIDER_MODULES = ["ngm.ngscrape.spiders"]
NEWSPIDER_MODULE = "ngm.ngscrape.spiders"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ROBOTSTXT_OBEY = True
CONCURRENT_REQUESTS = 2
DOWNLOAD_DELAY = 1
DOWNLOAD_TIMEOUT = 600
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

ENABLE_CAPTCHA_COOKIE_EXTRACT = True
TELNETCONSOLE_ENABLED = False

EXTENSIONS = {
   "ngm.ngscrape.extensions.SchemaChangeDetector": 500,
}

FEED_EXPORT_ENCODING = "utf-8"
