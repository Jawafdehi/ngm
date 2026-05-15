# Nepal Government Modernization (NGM)

**Judicial Data Scrapers for Nepal's Court System**

## Overview

Nepal Government Modernization (NGM) is a specialized data collection service that systematically scrapes and structures judicial data from Nepal's court system. The service collects case information, hearing records, and legal proceedings from all levels of Nepal's judiciary, making this public information accessible in a structured, queryable format.

## What NGM Does

NGM automates the collection of judicial data from Nepal's court websites, transforming unstructured web pages into a comprehensive database of court cases and proceedings. This enables:

- **Transparency**: Making court proceedings accessible to citizens, researchers, and journalists
- **Accountability**: Tracking case progression and judicial decisions over time
- **Research**: Enabling data-driven analysis of Nepal's judicial system
- **Integration**: Providing structured data for other services in the Jawafdehi ecosystem

## Data Sources

NGM collects data from all levels of Nepal's court system:

### Supreme Court (सर्वोच्च अदालत)
- The highest court in Nepal
- Final appellate jurisdiction
- Constitutional interpretation

### High Courts (उच्च अदालत)
- 18 High Courts across Nepal
- Appellate jurisdiction over district courts
- Original jurisdiction in certain matters

### District Courts (जिल्ला अदालत)
- 77 District Courts (one per district)
- Original jurisdiction for most civil and criminal cases
- First level of judicial proceedings

### Special Court (विशेष अदालत)
- Specialized court for corruption and financial crimes
- Critical for anti-corruption efforts
- High-profile cases involving public officials

## Data Collected

### Case Information
- **Case Numbers**: Unique identifiers (format: DDD-SS-DDDD)
- **Registration Details**: When and where cases were filed
- **Case Types**: Classification of legal matters (भ्रष्टाचार, चेक अनादर, etc.)
- **Parties**: Plaintiffs and defendants with addresses
- **Legal Sections**: Applicable laws and regulations
- **Case Status**: Current state (चालु, फैसला भएको, etc.)
- **Verdicts**: Final decisions and verdict dates

### Hearing Records
- **Hearing Dates**: When cases appear in court (BS and AD formats)
- **Bench Information**: Which judges are hearing the case
- **Bench Types**: Single bench (एकल इजलास) or joint bench (संयुक्त इजलास)
- **Judge Names**: Presiding judges for each hearing
- **Lawyer Information**: Legal representation for both sides
- **Hearing Outcomes**: Decisions, adjournments, and orders

### Entity Information
- **Party Details**: Structured information about plaintiffs and defendants
- **Addresses**: Geographic information for parties
- **Entity Resolution**: Links to Nepal Entity Service for standardized entity identification

## Architecture

### Scrapy Framework
NGM is built on Scrapy, a powerful web scraping framework that provides:
- Robust error handling and retry logic
- Concurrent request processing
- Middleware for custom processing
- Pipeline architecture for data transformation

### Database Schema
PostgreSQL database with four main tables:

1. **Courts**: Master table of all courts in Nepal
2. **Court Cases**: Case metadata and registration information
3. **Court Case Hearings**: Individual hearing records over time
4. **Case Entities**: Structured party information

### Spiders (Data Collectors)

NGM includes specialized spiders for each court type:

- `supreme_court_cases.py` - Supreme Court case listings
- `supreme_case_enrichment.py` - Detailed Supreme Court case information
- `high_court_cases.py` - High Court case listings
- `district_court_cases.py` - District Court case listings
- `district_case_enrichment.py` - Detailed District Court case information
- `special_court_cases.py` - Special Court case listings
- `special_case_enrichment.py` - Detailed Special Court case information
- `kanun_patrika.py` - Legal gazette scraper

### Two-Stage Collection Process

1. **Case Listing**: Scrape daily causelists to collect basic case information
2. **Case Enrichment**: Follow links to detail pages for comprehensive case data

This approach ensures efficient data collection while respecting server resources.

## Data Quality Features

### Date Handling
- **Dual Format Support**: Stores dates in both Bikram Sambat (BS) and Gregorian (AD) formats
- **Automatic Conversion**: Uses `nepali` library for accurate date conversion
- **Timezone Awareness**: Proper handling of Nepal timezone (UTC+5:45)

### Text Normalization
- **Unicode Standardization**: Consistent Nepali text representation
- **Whitespace Handling**: Proper trimming and formatting
- **Character Encoding**: UTF-8 throughout the pipeline

### Deduplication
- **Unique Constraints**: Case number + court identifier ensures no duplicates
- **Upsert Logic**: Updates existing records with new information
- **Hearing Tracking**: Multiple hearings for the same case properly linked

### Progress Tracking
- **Scraped Dates Table**: Tracks which dates have been successfully collected
- **Resume Capability**: Can restart from last successful scrape
- **Status Tracking**: Monitors enrichment status (pending, enriched, failed)

## Integration with Jawafdehi Ecosystem

### Nepal Entity Service (NES)
- Links case parties to standardized entities
- Enables cross-case entity tracking
- Supports entity-based search and analysis

### JawafdehiAPI
- Provides court case data for corruption tracking
- Enables case-based accountability features
- Supports public access to judicial information

### Research & Analysis
- Structured data enables statistical analysis
- Supports case outcome research
- Facilitates judicial performance studies

---

**Part of the Jawafdehi Project**: Nepal's open database for transparency and accountability.

**License**: [Hippocratic License 3.0](./LICENSE) — an [Ethical Source](https://ethicalsource.dev) license. See [LICENSING.md](./LICENSING.md) for rationale.

**Contact**: For questions or collaboration opportunities, please reach out through the Jawafdehi project channels.
# JAWA-1048
