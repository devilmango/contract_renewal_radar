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
    notice_day_type: Literal["calendar", "business"] = "calendar"
    notice_timezone: str = "UTC"
    notice_holidays: list[date] = Field(default_factory=list)
    notice_method: str | None = Field(default=None, max_length=250)
    notice_recipient: str | None = Field(default=None, max_length=500)
    auto_renew: bool | None = None
    termination_notice: str | None = Field(default=None, max_length=250)
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)
    supersedes_contract_id: str | None = Field(default=None, max_length=100)
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
    status: Literal["pending_review", "active", "rejected", "superseded", "redacted"]
    extraction_provider: str
    extracted: ContractData
    confirmed: ContractData | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    tenant_id: str = "default"


class ConfirmationRequest(BaseModel):
    contract: ContractData


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
    tenant_id: str = "default"
    notice_day_type: Literal["calendar", "business"] = "calendar"
    notice_timezone: str = "UTC"
    notice_holidays: list[date] = Field(default_factory=list)
    workflow_state: Literal["review", "needs_changes", "pending_approval", "notice_in_progress", "renewed", "terminated", "cancelled", "resolved"] = "review"


class ResolveTaskRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class TaskTransitionRequest(BaseModel):
    workflow_state: Literal["review", "needs_changes", "pending_approval", "notice_in_progress", "renewed", "terminated", "cancelled"]
    comment: str | None = Field(default=None, max_length=2000)


class TaskAssignmentRequest(BaseModel):
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)

    @field_validator("owner_email")
    @classmethod
    def validate_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value


class TaskCommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class TaskComment(BaseModel):
    id: int
    task_id: str
    actor: str
    body: str
    created_at: datetime


class NoticeCreateRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50000)
    recipient: str = Field(min_length=1, max_length=500)
    delivery_method: Literal["email", "registered_mail", "courier", "portal", "other"]


class NoticeDispatchRequest(BaseModel):
    sent_at: datetime | None = None
    delivery_reference: str = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=2000)


class NoticeDeliveryRequest(BaseModel):
    delivered_at: datetime | None = None
    delivery_reference: str | None = Field(default=None, max_length=500)
    evidence_note: str = Field(min_length=1, max_length=5000)


class NoticeRecord(BaseModel):
    id: str
    task_id: str
    status: Literal["draft", "approved", "dispatched", "delivered", "cancelled"]
    subject: str
    body: str
    recipient: str
    delivery_method: Literal["email", "registered_mail", "courier", "portal", "other"]
    created_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    dispatched_by: str | None
    dispatched_at: datetime | None
    delivered_at: datetime | None
    delivery_reference: str | None
    delivery_note: str | None


class JobRecord(BaseModel):
    id: str
    tenant_id: str
    job_type: str
    status: Literal["queued", "running", "succeeded", "dead"]
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error: str | None
    result: dict | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class LegalHoldRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class LegalHoldRecord(BaseModel):
    id: str
    contract_id: str
    reason: str
    placed_by: str
    placed_at: datetime
    released_by: str | None
    released_at: datetime | None


class AccessEvent(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    accessed_at: datetime
