from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
    annual_contract_value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
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


class IntegrationConnectionRequest(BaseModel):
    """Tenant-owned provider configuration; credentials are encrypted before persistence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["google_calendar", "microsoft_calendar", "smtp", "microsoft_email", "gmail"]
    settings: dict[str, str] = Field(default_factory=dict, max_length=20)
    credentials: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("settings", "credentials")
    @classmethod
    def validate_connection_values(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 80 or len(item) > 4000 for key, item in value.items()):
            raise ValueError("integration keys must be short and values cannot exceed 4000 characters")
        return value


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


class EscalationStep(BaseModel):
    after_due_days: int = Field(ge=0, le=3650)
    recipient_email: str | None = Field(default=None, max_length=320)
    label: str = Field(default="Escalation", min_length=1, max_length=100)

    @field_validator("recipient_email")
    @classmethod
    def validate_recipient_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("recipient_email must be a valid email address")
        return value


class EscalationPolicy(BaseModel):
    reminder_days_before: list[int] = Field(default_factory=lambda: [0], max_length=12)
    escalation_steps: list[EscalationStep] = Field(default_factory=list, max_length=10)
    backup_owner_email: str | None = Field(default=None, max_length=320)

    @field_validator("backup_owner_email")
    @classmethod
    def validate_backup_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("backup_owner_email must be a valid email address")
        return value

    @field_validator("reminder_days_before")
    @classmethod
    def validate_reminder_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 3650 for day in value):
            raise ValueError("reminder days must be between 0 and 3650")
        if len(set(value)) != len(value):
            raise ValueError("reminder days must be unique")
        return sorted(value, reverse=True)

    @field_validator("escalation_steps")
    @classmethod
    def validate_escalation_steps(cls, value: list[EscalationStep]) -> list[EscalationStep]:
        days = [step.after_due_days for step in value]
        if len(set(days)) != len(days):
            raise ValueError("escalation steps must have unique after_due_days values")
        return sorted(value, key=lambda step: step.after_due_days)


class EvaluationFeedbackApprovalRequest(BaseModel):
    redacted_text: str = Field(min_length=40, max_length=200000)
    approval_reference: str = Field(min_length=1, max_length=200)
    fields: list[Literal[
        "start_date", "expiration_date", "renewal_notice_days", "notice_day_type", "auto_renew", "termination_notice",
    ]] = Field(min_length=1, max_length=6)
    clause_categories: list[Literal[
        "effective_term", "expiration", "renewal", "auto_renewal", "notice_period", "termination",
    ]] = Field(min_length=1, max_length=12)
    evidence: list[Evidence] = Field(min_length=1, max_length=50)
    attest_approved: Literal[True]
    attest_deidentified: Literal[True]


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


class RenewalOutcomeRequest(BaseModel):
    outcome: Literal["renewed", "renegotiated", "terminated", "expired", "no_action"]
    actual_annual_value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    annual_savings: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    successor_contract_id: str | None = Field(default=None, max_length=100)
    actual_expiration_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_amount_currency(self) -> "RenewalOutcomeRequest":
        if (self.actual_annual_value is not None or self.annual_savings is not None) and not self.currency:
            raise ValueError("currency is required when recording an actual value or savings")
        if self.outcome in {"renewed", "renegotiated"} and self.actual_expiration_date is None:
            raise ValueError("actual_expiration_date is required for renewed or renegotiated contracts")
        return self


class SupplierCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=250)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Supplier name cannot be blank")
        return value.strip()


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
