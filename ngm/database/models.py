"""
SQLAlchemy models for Nepal court case database.

This module defines the database schema for storing court cases and hearings
from Nepal's court system (district, high, supreme, and special courts).
"""

import os
from datetime import datetime
from sqlalchemy import (
    Column,
    String,
    Date,
    DateTime,
    Text,
    Integer,
    ForeignKey,
    create_engine,
    Index,
)
from sqlalchemy.orm import relationship, declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()


class Court(Base):
    """
    Court master table storing information about all courts in Nepal.

    This includes:
    - 1 Supreme Court
    - 1 Special Court
    - 18 High Courts
    - 77 District Courts
    """

    __tablename__ = "courts"

    # Primary identification
    identifier = Column(String(50), primary_key=True, nullable=False)
    # Examples: "kathmandudc", "rajbirajhc", "supreme", "special"

    # Court information
    court_type = Column(String(20), nullable=False, index=True)
    # Values: "district", "high", "supreme", "special"

    full_name_nepali = Column(String(200), nullable=False)
    # Example: "जिल्ला अदालत काठमाडौं"

    full_name_english = Column(String(200), nullable=True)
    # Example: "District Court Kathmandu"

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<Court(identifier={self.identifier}, type={self.court_type}, name={self.full_name_nepali})>"


class CourtCase(Base):
    """
    Main court case table storing case metadata and registration information.

    Each case is uniquely identified by case_number + court_identifier.
    A case can have multiple hearings over time tracked in CourtCaseHearing table.

    Field Usage by Court Type:

    District Courts:
    - case_type: मुद्दाको किसिम (case type)
    - section: धारा (legal section)
    - priority: प्राथमिकता (priority/fast-track status)
    - case_id: Internal court ID

    Special Court:
    - case_type: मुद्दा (case type)
    - category: मुद्दाको किसिम (case category)
    - division: फाँट (division assignment, e.g., "फाँट ख")
    - case_status: मुद्दाको स्थिती (case status with verdict date if applicable)
    - extra_data: Stores special court specific fields:
      - pesi_tarekh: Array of hearing schedule dates
      - sadharan_tarekh: Array of regular dates
      - related_cases: Array of related case information
      - plaintiff_advocates: Plaintiff lawyer names
      - defendant_advocates: Defendant lawyer names
      - judges_excluded: List of judges who cannot hear the case

    Supreme/High Courts:
    - Similar to special court with court-specific variations

    Enrichment Status:
    - status: "pending" (not enriched), "enriched" (detail scraped), "failed" (enrichment failed)
    - When status="enriched", additional fields are populated from detail pages
    """

    __tablename__ = "court_cases"

    # Primary identification
    case_number = Column(String(50), primary_key=True, nullable=False, index=True)
    # Example: "082-OA-0503", "081-C4-3088"

    court_identifier = Column(
        String(50),
        ForeignKey("courts.identifier"),
        primary_key=True,
        nullable=False,
        index=True,
    )

    # Relationship
    court = relationship("Court", backref="cases")

    # Registration information
    registration_date_bs = Column(String(20), nullable=True, index=True)
    # BS format: "2082-09-28"

    registration_date_ad = Column(Date, nullable=True, index=True)
    # AD format for queries

    # Case classification
    case_type = Column(String(200), nullable=True, index=True)
    # Example: "भ्रष्टाचार ( रिसवत(घुस) )", "चेक अनादर"

    # फाट
    division = Column(String(100), nullable=True)
    # Example: "निवेदन ४", "रिट १"

    # bench_type
    category = Column(String(100), nullable=True)
    # Example: "फाँट क", "फाँट ख"

    section = Column(String(200), nullable=True)
    # Example: "मुद्दा फाटँ २७ (सरल)"

    # Parties
    plaintiff = Column(Text, nullable=True)
    # Can be multiple parties separated by "समेत"

    defendant = Column(Text, nullable=True)
    # Can be multiple parties separated by "समेत"

    # Related case information
    original_case_number = Column(String(100), nullable=True)
    # For appeals/related cases

    case_id = Column(String(50), nullable=True)
    # Internal court ID (district courts)

    # Priority and processing
    priority = Column(String(400), nullable=True)
    # Example: "सरल" (simple/fast-track)

    # Enriched case information (from detail page scraping)
    registration_number = Column(String(100), nullable=True)

    case_status = Column(String(100), nullable=True, index=True)
    # Example: "चालु", "फैसला भएको"

    verdict_date_bs = Column(String(20), nullable=True)
    # BS format: "2082-09-28" or "**** ** **" if not available

    verdict_date_ad = Column(Date, nullable=True)
    # AD format for queries

    verdict_judge = Column(String(500), nullable=True)
    # Judge who gave the verdict

    verdict_type = Column(String(100), nullable=True)
    # Example: "फैसला", "आदेश" — kind of the final decision (supreme)

    case_subject = Column(Text, nullable=True)
    # मुद्दाको बिषय — full case subject text (district/supreme detail pages)

    hearing_count = Column(String(20), nullable=True)
    # पेशी चढेको संख्या — number of times the case reached a hearing

    enriched_at = Column(DateTime, nullable=True, index=True)
    # When the detail page was successfully scraped (distinct from updated_at)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Enrichment status
    status = Column(String(20), nullable=True, default="pending", index=True)
    # Values: "pending", "enriched", "failed"
    # Tracks whether detailed case information has been scraped

    # Additional data (case-specific metadata)
    extra_data = Column(JSONB, nullable=True)

    def __repr__(self):
        return f"<CourtCase(case_number={self.case_number}, court={self.court_identifier})>"


class CourtCaseHearing(Base):
    """
    Court case hearing records - each row represents one appearance in the daily causelist.

    A case can have multiple hearings over time. This table tracks the progression
    of the case through the court system.
    """

    __tablename__ = "court_case_hearings"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Foreign key to CourtCase (composite)
    case_number = Column(String(50), nullable=False, index=True)

    court_identifier = Column(
        String(50), ForeignKey("courts.identifier"), nullable=False, index=True
    )

    # Relationship
    court = relationship("Court", backref="hearings")

    # Hearing date
    hearing_date_bs = Column(String(20), nullable=False, index=True)
    # BS format: "2082-09-28"

    hearing_date_ad = Column(Date, nullable=False, index=True)
    # AD format for queries

    # Bench information
    bench = Column(String(100), nullable=True)
    # Example: "इजलाश 31", "इजलास नं १"

    bench_type = Column(String(100), nullable=True)
    # Example: "संयुक्त इजलास", "एकल इजलास"

    # Judges - simple text field, can contain multiple judges separated by newlines
    judge_names = Column(Text, nullable=True)
    # Example: "माननीय न्यायाधीश श्री कृतबहादुर वोहरा"
    # Or: "अध्यक्ष माननीय न्यायाधीश श्री सुदर्शनदेव भट्ट\n सदस्य माननीय न्यायाधीश श्री हेमन्त रावल"

    # Lawyers - simple text field
    lawyer_names = Column(Text, nullable=True)
    # Can list plaintiff and defendant lawyers

    # Hearing details
    serial_no = Column(String(20), nullable=True)
    # Order in causelist (क, ख, 1, 2, etc.)

    # Status and decisions
    case_status = Column(String(100), nullable=True)
    # Example: "स्थगित", "आदेश", "फैसला"

    decision_type = Column(String(200), nullable=True)
    # Example: "थुनछेक आदेश (धरौटी)", "ठहर"

    remarks = Column(Text, nullable=True)
    # Example: "थुनछेक", "बृद्ध"

    # Audit fields
    scraped_at = Column(DateTime, nullable=False)
    # When this record was scraped

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Additional data (court-specific fields stored as JSON)
    # extra_data may include:
    # - bench_id: Internal bench ID (high courts)
    # - bench_no: Bench number
    # - bench_label: Judge names summary (special court)
    # - court_number: Court room number
    # - judges: Array of judge objects with roles (for structured data)
    # - lawyers: Object with plaintiff/defendant lawyers (for structured data)
    # - judges_cannot_hear: Supreme court specific field
    # - status: Multi-line status field (high courts)
    # - footer: Court officer details
    # - Any other court-specific fields
    extra_data = Column(JSONB, nullable=True)

    def __repr__(self):
        return f"<CourtCaseHearing(case_number={self.case_number}, hearing_date={self.hearing_date_bs})>"


class CaseEntity(Base):
    """
    Case entities table storing plaintiff and defendant information.

    This table stores detailed party information extracted from case detail pages.
    Each row represents one party (plaintiff or defendant) in a case.
    """

    __tablename__ = "court_case_entities"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Foreign key to CourtCase (composite)
    case_number = Column(String(50), nullable=False, index=True)

    court_identifier = Column(
        String(50), ForeignKey("courts.identifier"), nullable=False, index=True
    )

    # Relationship
    court = relationship("Court", backref="case_entities")

    # Party information
    side = Column(String(20), nullable=False, index=True)
    # Values: "plaintiff", "defendant"

    name = Column(String(500), nullable=False)
    # Party name (required)

    address = Column(String(500), nullable=True)
    # Party address (optional)

    # External entity resolution
    nes_id = Column(String(100), nullable=True, index=True)
    # Nepal Entity Service ID for entity resolution

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<CaseEntity(case_number={self.case_number}, side={self.side}, name={self.name})>"


# Indexes for performance
Index("idx_court_type", Court.court_type)

Index("idx_case_court_date", CourtCase.court_identifier, CourtCase.registration_date_ad)

Index(
    "idx_hearing_court_date",
    CourtCaseHearing.court_identifier,
    CourtCaseHearing.hearing_date_ad,
)

Index("idx_case_type_court", CourtCase.case_type, CourtCase.court_identifier)

Index(
    "idx_hearing_status", CourtCaseHearing.case_status, CourtCaseHearing.hearing_date_ad
)

# Full-text search indexes (PostgreSQL specific)
# These enable fast text search on names
# Note: Requires pg_trgm extension: CREATE EXTENSION IF NOT EXISTS pg_trgm;
Index(
    "idx_case_plaintiff_fts",
    CourtCase.plaintiff,
    postgresql_using="gin",
    postgresql_ops={"plaintiff": "gin_trgm_ops"},
)

Index(
    "idx_case_defendant_fts",
    CourtCase.defendant,
    postgresql_using="gin",
    postgresql_ops={"defendant": "gin_trgm_ops"},
)

Index(
    "idx_hearing_judge_fts",
    CourtCaseHearing.judge_names,
    postgresql_using="gin",
    postgresql_ops={"judge_names": "gin_trgm_ops"},
)

# Case entity indexes
Index("idx_case_entity_case", CaseEntity.case_number, CaseEntity.court_identifier)

Index("idx_case_entity_side", CaseEntity.side, CaseEntity.court_identifier)

Index(
    "idx_case_entity_name_fts",
    CaseEntity.name,
    postgresql_using="gin",
    postgresql_ops={"name": "gin_trgm_ops"},
)

# Case status index for enrichment queries
Index("idx_case_status_date", CourtCase.status, CourtCase.registration_date_ad.desc())


class CourtScrapedDate(Base):
    """
    Track which dates have been successfully scraped for each court.

    This allows spiders to resume from where they left off and avoid
    re-scraping dates that have already been processed.
    """

    __tablename__ = "scraped_dates"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Court identifier
    court_identifier = Column(
        String(50), ForeignKey("courts.identifier"), nullable=False, index=True
    )

    # Relationship
    court = relationship("Court", backref="scraped_dates")

    # Date that was scraped (BS format)
    date_bs = Column(String(20), nullable=False, index=True)

    data = Column(Text, nullable=True)

    # Audit fields
    scraped_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<CourtScrapedDate(court={self.court_identifier}, date_bs={self.date_bs})>"
        )


# Unique constraint: one record per court per date
Index(
    "idx_scraped_date_unique",
    CourtScrapedDate.court_identifier,
    CourtScrapedDate.date_bs,
    unique=True,
)


class BlacklistedFirm(Base):
    """Firms blacklisted by PPMO (Public Procurement Monitoring Office)."""

    __tablename__ = "blacklisted_firms"

    id = Column(Integer, primary_key=True, autoincrement=True)

    firm_name = Column(String(500), nullable=False, index=True)
    proprietor_name = Column(String(500), nullable=True)
    address = Column(String(500), nullable=True)

    blacklist_date_bs = Column(String(20), nullable=True)
    blacklist_date_ad = Column(Date, nullable=True, index=True)

    effective_until_bs = Column(String(20), nullable=True)
    effective_until_ad = Column(Date, nullable=True, index=True)

    duration = Column(String(100), nullable=True)
    reason = Column(Text, nullable=True)
    recommending_office = Column(String(500), nullable=True)

    nes_id = Column(String(100), nullable=True, index=True)

    scraped_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return (
            f"<BlacklistedFirm(name={self.firm_name}, until={self.effective_until_bs})>"
        )


Index(
    "idx_blacklisted_firm_unique",
    BlacklistedFirm.firm_name,
    BlacklistedFirm.blacklist_date_bs,
    unique=True,
)


# Database connection helpers

# Global engine instance (singleton pattern)
_engine = None
_engine_url = None


def _build_ssl_connect_args():
    """
    Build SSL connect_args from environment variables for TLS/SSL database connections.

    Reads PGSSLMODE and PGSSLROOTCERT environment variables and returns a dict
    suitable for passing as connect_args to SQLAlchemy's create_engine().

    Returns:
        dict: SSL connect_args dict (empty if no SSL env vars are set)
    """
    connect_args = {}
    if os.getenv("PGSSLMODE"):
        connect_args["sslmode"] = os.getenv("PGSSLMODE")
    if os.getenv("PGSSLROOTCERT"):
        connect_args["sslrootcert"] = os.getenv("PGSSLROOTCERT")
    return connect_args


def get_engine(database_url=None):
    """
    Get or create database engine (singleton pattern).

    Returns the same engine instance across calls. Once an engine is created,
    all subsequent calls must use the same database URL.

    SSL/TLS is configured via environment variables:
        PGSSLMODE: e.g., "verify-ca", "require", "disable"
        PGSSLROOTCERT: path to CA certificate file

    Args:
        database_url: PostgreSQL connection string. If None, reads from DATABASE_URL env var.

    Returns:
        SQLAlchemy engine instance

    Raises:
        ValueError: If database_url is not provided and DATABASE_URL env var is not set
        AssertionError: If called with a different database_url after engine is created

    Example:
        engine = get_engine('postgresql://user:password@localhost:5432/court_cases')
    """
    global _engine, _engine_url

    if database_url is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError(
                "DATABASE_URL not provided and not found in environment variables. "
                "Please set DATABASE_URL or pass database_url parameter."
            )

    # If engine already exists, assert URL is the same
    if _engine is not None:
        assert _engine_url == database_url, (
            f"Attempted to create engine with different database URL. "
            f"Existing: {_engine_url}, Requested: {database_url}. "
            f"Only one database URL is allowed per process."
        )
        return _engine

    # Create new engine with SSL connect_args. pool_pre_ping recycles a stale
    # connection (e.g. one the server dropped while idle) instead of erroring on
    # first use — important for the long-running scrapers against CNPG.
    connect_args = _build_ssl_connect_args()
    _engine = create_engine(
        database_url, connect_args=connect_args, echo=False, pool_pre_ping=True
    )
    _engine_url = database_url

    return _engine


def get_session(engine):
    """
    Create database session with explicit transaction control.

    Args:
        engine: SQLAlchemy engine instance

    Returns:
        SQLAlchemy session instance

    Example:
        engine = get_engine()
        session = get_session(engine)

        # Use session with explicit transactions
        with session.begin():
            courts = session.query(Court).all()

        # Close when done
        session.close()
    """
    Session = sessionmaker(bind=engine, autobegin=False)
    return Session()


def init_db(engine):
    """
    Initialize database tables.

    Creates all tables defined in the models if they don't exist.
    Safe to run multiple times (won't drop existing tables).

    Args:
        engine: SQLAlchemy engine instance

    Example:
        engine = get_engine()
        init_db(engine)
    """
    Base.metadata.create_all(engine)


def drop_all_tables(engine):
    """
    Drop all tables from the database.

    WARNING: This will delete all data! Use with caution.

    Args:
        engine: SQLAlchemy engine instance

    Example:
        engine = get_engine()
        drop_all_tables(engine)
    """
    Base.metadata.drop_all(engine)


# Example usage
if __name__ == "__main__":
    """
    Example usage of the models.

    Run with:
        DATABASE_URL='postgresql://user:password@localhost:5432/court_cases' poetry run python ngm/models.py
    """
    import sys

    # Get database URL
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set")
        print(
            "Usage: DATABASE_URL='postgresql://user:pass@host:5432/db' poetry run python ngm/models.py"
        )
        sys.exit(1)

    print("Connecting to database...")
    engine = get_engine(database_url)

    print("Creating tables...")
    init_db(engine)

    print("Tables created successfully!")

    # Show table information
    session = get_session(engine)

    print("\nDatabase statistics:")
    print(f"  Courts: {session.query(Court).count()}")
    print(f"  Cases: {session.query(CourtCase).count()}")
    print(f"  Hearings: {session.query(CourtCaseHearing).count()}")

    session.close()
    engine.dispose()

    print("\n✓ Done!")
