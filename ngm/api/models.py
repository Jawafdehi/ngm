"""Pydantic models for API responses."""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class CaseEntityResponse(BaseModel):
    """Case entity (plaintiff or defendant) response model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    side: str = Field(description="Either 'plaintiff' or 'defendant'")
    name: Optional[str] = None
    address: Optional[str] = None
    nes_id: Optional[str] = Field(None, description="Nepal Entity Service ID")


class HearingResponse(BaseModel):
    """Court case hearing response model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    hearing_date_bs: str = Field(description="Hearing date in Bikram Sambat format")
    hearing_date_ad: date = Field(description="Hearing date in AD format")
    bench: Optional[str] = None
    bench_type: Optional[str] = None
    judge_names: Optional[str] = None
    lawyer_names: Optional[str] = None
    serial_no: Optional[str] = None
    case_status: Optional[str] = None
    decision_type: Optional[str] = None
    remarks: Optional[str] = None
    scraped_at: datetime
    extra_data: Optional[dict] = None


class CourtCaseDetailResponse(BaseModel):
    """Complete court case detail response including hearings and entities."""

    model_config = ConfigDict(from_attributes=True)

    case_number: str
    court_identifier: str
    registration_date_bs: Optional[str] = None
    registration_date_ad: Optional[date] = None
    case_type: Optional[str] = None
    division: Optional[str] = None
    category: Optional[str] = None
    section: Optional[str] = None
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    original_case_number: Optional[str] = None
    case_id: Optional[str] = None
    priority: Optional[str] = None
    registration_number: Optional[str] = None
    case_status: Optional[str] = None
    verdict_date_bs: Optional[str] = None
    verdict_date_ad: Optional[date] = None
    verdict_judge: Optional[str] = None
    status: Optional[str] = None
    extra_data: Optional[dict] = None
    created_at: datetime
    updated_at: datetime

    # Related data
    hearings: list[HearingResponse] = Field(default_factory=list)
    entities: list[CaseEntityResponse] = Field(default_factory=list)
