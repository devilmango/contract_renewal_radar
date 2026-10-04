from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Evidence(BaseModel):
    field: str
    quote: str = Field(min_length=1, max_length=1000)
    page: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.5, ge=0, le=1)


class ContractData(BaseModel):
    """Normalized contract terms. Dates use ISO-8601 calendar dates."""

    model_config = ConfigDict(str_strip_whitespace=True)

    contract: str | None = Field(default=None, max_length=250)
    start_date: date | None = None
    expiration_date: date | None = None
    renewal_notice_days: int | None = Field(default=None, ge=0, le=3650)
    auto_renew: bool | None = None
    termination_notice: str | None = Field(default=None, max_length=250)
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("owner_email")
    @classmethod
    def validate_email_shape(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value

    @model_validator(mode="after")
    def validate_date_order(self) -> "ContractData":
        if self.start_date and self.expiration_date and self.expiration_date <= self.start_date:
            raise ValueError("expiration_date must be after start_date")
        return self


class ContractRecord(BaseModel):
    id: str
    filename: str
    status: Literal["pending_review", "active", "rejected"]
    extraction_provider: str
    extracted: ContractData
    confirmed: ContractData | None = None
    created_at: datetime
    confirmed_at: datetime | None = None


class ConfirmationRequest(BaseModel):
    contract: ContractData
    actor: str = Field(min_length=1, max_length=150)


class AuditEvent(BaseModel):
    id: int
    contract_id: str
    event_type: str
    actor: str
    details: dict
    created_at: datetime


class ReminderTask(BaseModel):
    id: str
    contract_id: str
    title: str
    owner_name: str | None
    owner_email: str | None
    due_date: date
    expiration_date: date
    status: Literal["open", "resolved"]
    reminder_sent_at: datetime | None
    escalated_at: datetime | None
    created_at: datetime


class ResolveTaskRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=150)
    comment: str | None = Field(default=None, max_length=2000)
