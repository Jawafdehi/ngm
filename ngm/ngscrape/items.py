import scrapy

class BlacklistedFirmItem(scrapy.Item):
    firm_name = scrapy.Field()
    proprietor_name = scrapy.Field()
    address = scrapy.Field()
    blacklist_date_bs = scrapy.Field()
    blacklist_date_ad = scrapy.Field()
    effective_until_bs = scrapy.Field()
    effective_until_ad = scrapy.Field()
    duration = scrapy.Field()
    reason = scrapy.Field()
    recommending_office = scrapy.Field()
    source_url = scrapy.Field()
    
    # NES mapping fields
    entity_type = scrapy.Field()
    entity_sub_type = scrapy.Field()
    names = scrapy.Field()
