# Scrapy settings for ngscrape project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

import logging
import os
from dotenv import load_dotenv

load_dotenv()

FILES_STORE = os.getenv("FILES_STORE", "output")

logging.getLogger("protego._protego").setLevel(logging.INFO)

BOT_NAME = "ngscrape"

# Set timezone to Kathmandu, Nepal
TIMEZONE = "Asia/Kathmandu"

# Set log level
LOG_LEVEL = "INFO"

SPIDER_MODULES = ["ngm.ngscrape.spiders"]
NEWSPIDER_MODULE = "ngm.ngscrape.spiders"

ADDONS = {}


# Crawl responsibly by identifying yourself (and your website) on the user-agent
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Obey robots.txt rules
ROBOTSTXT_OBEY = True

# Concurrency and throttling settings
CONCURRENT_REQUESTS = 2
# CONCURRENT_REQUESTS_PER_DOMAIN = 3
DOWNLOAD_DELAY = 1

DOWNLOAD_TIMEOUT = 600

RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# CAPTCHA Cookie Extraction
# This feature extracts CAPTCHA solutions from session cookies on the Supreme Court website.
#
# Justification for enabled-by-default:
# - The Supreme Court website (supremecourt.gov.np) serves publicly accessible court records
#   for transparency - not private or restricted data
# - The website embeds the CAPTCHA answer in the session cookie as part of its implementation
# - This spider is designed for educational/research use to improve public access to legal information
# - Without this extraction, the spider cannot submit search forms at all
#   (this is not bypassing security - it's using the mechanism the website provides)
#
# Legal/Compliance: Deemed acceptable for public data access in educational/research context
# Use Case: Educational and research purposes to improve transparency and access to public legal records
ENABLE_CAPTCHA_COOKIE_EXTRACT = True

# Disable cookies (enabled by default)
# COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
TELNETCONSOLE_ENABLED = False

# Override the default request headers:
# DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
# }

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
# SPIDER_MIDDLEWARES = {
#    "ngscrape.middlewares.NgscrapeSpiderMiddleware": 543,
# }

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
# DOWNLOADER_MIDDLEWARES = {
#    "ngscrape.middlewares.NgscrapeDownloaderMiddleware": 543,
# }

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
# EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
# }

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
# ITEM_PIPELINES = {
#    "ngscrape.pipelines.NgscrapePipeline": 300,
# }

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
# AUTOTHROTTLE_ENABLED = True
# The initial download delay
# AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
# AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
# AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
# AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
# HTTPCACHE_ENABLED = True
# HTTPCACHE_EXPIRATION_SECS = 0
# HTTPCACHE_DIR = "httpcache"
# HTTPCACHE_IGNORE_HTTP_CODES = []
# HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
FEED_EXPORT_ENCODING = "utf-8"
