"""Pydantic models for the CIAA Cases Dataset."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class CourtCaseRecord(BaseModel):
    court: str  # "special" | "supreme" | ...
    case_no: str
    registration_date_bs: Optional[str] = None
    registration_date_ad: Optional[str] = None
    defendants: list[Defendant] = []
    current_status: Optional[str] = None
    faisala_link: list[str] = []


class PressReleaseRecord(BaseModel):
    release_id: int
    url: str
    r2_metadata_url: Optional[str] = None
    date: Optional[str] = None
    title: str


class AbhiyogPatraRecord(BaseModel):
    case_number: str
    title: str
    filing_date: Optional[str] = None
    pdf_url: Optional[str] = None
    court_office: Optional[str] = None


class CIAABlock(BaseModel):
    press_releases: list[PressReleaseRecord] = []
    abhiyogPatras: list[AbhiyogPatraRecord] = []


class CaseMeta(BaseModel):
    match_confidence: float
    match_signals: list[str] = []
    unmatched_reason: Optional[str] = None
    match_status: str  # "confirmed" | "needs_review" | "unmatched"


class CIAACase(BaseModel):
    case_no: str
    case_title: str
    fiscal_year: str  # e.g. "80-81"
    jawafdehi_case_url: Optional[str] = None
    ciaa: CIAABlock = CIAABlock()
    court_case: CourtCaseRecord
    appealed_case: Optional[CourtCaseRecord] = None
    meta: CaseMeta

    class Config:
        populate_by_name = True


class Defendant(BaseModel):
    name: str


class MatchResult(BaseModel):
    press_releases: list[PressReleaseRecord] = []
    charge_sheets: list[AbhiyogPatraRecord] = []
    confidence: float = 0.0
    match_signals: list[str] = []
    match_status: str = "unmatched"  # "confirmed" | "needs_review" | "unmatched"
    unmatched_reason: Optional[str] = None
    llm_defer_data: Optional[dict] = None  # For batch LLM processing


class ValidationResult(BaseModel):
    is_valid: bool
    errors: list[str] = []
    warnings: list[str] = []
