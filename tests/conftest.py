"""Shared fixtures for NGM scraper tests.

The small unit tests under ``tests/unit/`` are pure (no DB). The larger parser
tests use the in-memory SQLite ``session`` fixture here. ``models.py`` only needs
one tweak to run on SQLite: its ``extra_data`` columns are Postgres ``JSONB`` —
we rebind those to dialect-agnostic ``JSON`` before ``create_all``. The four
``pg_trgm`` GIN indexes carry ``postgresql_using=``, so SQLAlchemy already skips
them on the sqlite dialect. There are no real ``ARRAY`` columns (the "array"
fields live inside ``extra_data`` JSON).
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.orm import sessionmaker

from ngm.database.models import Base, Court
from ngm.utils.db_helpers import CaseCache

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "html"


def _patch_jsonb_to_json():
    """Rebind Postgres JSONB columns to dialect-agnostic JSON (idempotent)."""
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.type.__class__.__name__ == "JSONB":
                col.type = JSON()


@pytest.fixture()
def engine():
    """Fresh in-memory SQLite engine, FK enforcement on, tables created."""
    _patch_jsonb_to_json()
    eng = create_engine("sqlite://", future=True)

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    """Session with the courts referenced by fixtures pre-seeded."""
    Session = sessionmaker(bind=engine, autobegin=False, future=True)
    s = Session()
    with s.begin():
        for ident, ctype, name in [
            ("supreme", "supreme", "सर्वोच्च अदालत"),
            ("special", "special", "विशेष अदालत"),
            ("kathmandudc", "district", "जिल्ला अदालत काठमाडौं"),
            ("biratnagarhc", "high", "उच्च अदालत विराटनगर"),
        ]:
            s.add(Court(identifier=ident, court_type=ctype, full_name_nepali=name))
    yield s
    s.close()


@pytest.fixture()
def soup():
    """Load a fixture HTML file into BeautifulSoup (html.parser, like the spiders)."""

    def _load(name):
        return BeautifulSoup((FIXTURE_DIR / name).read_text("utf-8"), "html.parser")

    return _load


def make_spider(spider_cls, session):
    """Build a spider WITHOUT its DB-touching ``__init__``/``start``.

    Spider ``__init__`` calls ``get_engine()`` (a process-global singleton) and
    ``init_db``. For parser tests we want the test's SQLite session instead, so
    we allocate via ``__new__`` and wire only the attributes the parse methods
    use. (Once the spiders inherit ``BaseScrapeSpider``, this can call
    ``_init_db("sqlite://")`` instead.)
    """
    sp = spider_cls.__new__(spider_cls)
    sp.session = session
    sp.case_cache = CaseCache()
    sp._bench_counter = {}
    sp._data_by_date = {}
    sp.bench_types_by_date = {}
    return sp
