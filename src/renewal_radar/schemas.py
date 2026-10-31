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


class ContractObligation(BaseModel):
    """An extracted, schedulable obligation proposal with source evidence."""

    model_config = ConfigDict(str_strip_whitespace=True)

    category: Literal["price_increase", "insurance_certificate", "audit_right", "termination_window", "other"]
    title: str = Field(min_length=1, max_length=250)
    description: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    due_offset_days: int | None = Field(default=None, ge=0, le=3650)
    due_offset_from: Literal["start_date", "expiration_date"] | None = None
    due_offset_direction: Literal["before", "after"] | None = None
    recurrence_interval_months: int | None = Field(default=None, ge=1, le=120)
    evidence: Evidence

    @model_validator(mode="after")
    def validate_schedule(self) -> "ContractObligation":
        has_absolute = self.due_date is not None
        relative = (self.due_offset_days, self.due_offset_from, self.due_offset_direction)
        has_relative = all(value is not None for value in relative)
        if has_absolute == has_relative:
            raise ValueError("Provide either a due_date or a complete due offset and anchor")
        if any(value is not None for value in relative) and not has_relative:
            raise ValueError("Relative due dates require offset days, anchor, and direction")
        return self


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
    notice_jurisdiction: str | None = Field(default=None, max_length=32)
    notice_weekend_days: list[int] = Field(default_factory=lambda: [5, 6], max_length=7)
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
    obligations: list[ContractObligation] = Field(default_factory=list, max_length=100)

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

    @field_validator("notice_weekend_days")
    @classmethod
    def validate_weekend_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("notice_weekend_days must contain unique weekday numbers from 0 (Monday) to 6 (Sunday)")
        return value


class IntegrationConnectionRequest(BaseModel):
    """Tenant-owned provider configuration; credentials are encrypted before persistence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["google_calendar", "microsoft_calendar", "smtp", "microsoft_email", "gmail", "slack_webhook", "teams_webhook", "coupa", "sap_ariba"]
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


class ContractRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    related_contract_id: str = Field(min_length=1, max_length=100)
    relationship_type: Literal["master_of", "sow_under", "amendment_of", "renewal_of", "related_to"]
    note: str | None = Field(default=None, max_length=1000)


class DeadlineCalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expiration_date: date
    notice_days: int = Field(ge=0, le=3650)
    day_type: Literal["calendar", "business"] = "calendar"
    timezone: str = Field(default="UTC", max_length=64)
    jurisdiction: str | None = Field(default=None, max_length=32)
    holidays: list[date] = Field(default_factory=list, max_length=500)
    weekend_days: list[int] = Field(default_factory=lambda: [5, 6], max_length=7)

    @field_validator("weekend_days")
    @classmethod
    def validate_weekend_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("weekend_days must contain unique weekday numbers from 0 (Monday) to 6 (Sunday)")
        return value


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
    reminder_recipients: list[str] = Field(default_factory=list, max_length=50)
    escalation_recipients: list[str] = Field(default_factory=list, max_length=50)
    approval_recipients: list[str] = Field(default_factory=list, max_length=50)
    approval_escalation_recipients: list[str] = Field(default_factory=list, max_length=50)
    reminder_channels: list[Literal["email", "slack", "teams", "log"]] = Field(default_factory=lambda: ["email"], max_length=4)
    escalation_channels: list[Literal["email", "slack", "teams", "log"]] = Field(default_factory=lambda: ["email"], max_length=4)
    approval_channels: list[Literal["email", "slack", "teams", "log"]] = Field(default_factory=lambda: ["email"], max_length=4)
    approval_escalation_channels: list[Literal["email", "slack", "teams", "log"]] = Field(default_factory=lambda: ["email"], max_length=4)

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

    @field_validator("reminder_recipients", "escalation_recipients", "approval_recipients", "approval_escalation_recipients")
    @classmethod
    def validate_policy_recipients(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or "@" not in value or value.startswith("@") or value.endswith("@") for value in cleaned):
            raise ValueError("notification recipients must be valid email addresses")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("notification recipients must be unique")
        return cleaned

    @field_validator("reminder_channels", "escalation_channels", "approval_channels", "approval_escalation_channels")
    @classmethod
    def validate_policy_channels(cls, values: list[str]) -> list[str]:
        if not values or len(set(values)) != len(values):
            raise ValueError("notification channels must be non-empty and unique")
        return values


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
    notice_jurisdiction: str | None = None
    deadline_calculation: dict = Field(default_factory=dict)
    workflow_state: Literal["review", "needs_changes", "pending_approval", "notice_in_progress", "renewed", "terminated", "cancelled", "resolved"] = "review"
    kind: Literal["renewal", "obligation"] = "renewal"


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


class RenewalDecisionCreateRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=100)
    strategy: Literal["negotiate", "renew", "terminate"]
    reason: str = Field(min_length=1, max_length=5000)
    forecast_id: str | None = Field(default=None, max_length=100)
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)
    target_date: date | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A decision reason is required")
        return value.strip()

    @field_validator("owner_email")
    @classmethod
    def validate_decision_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value


class RenewalDecisionUpdateRequest(BaseModel):
    strategy: Literal["negotiate", "renew", "terminate"] | None = None
    reason: str | None = Field(default=None, max_length=5000)
    forecast_id: str | None = Field(default=None, max_length=100)
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)
    target_date: date | None = None

    @field_validator("reason")
    @classmethod
    def validate_updated_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("A decision reason cannot be blank")
        return value.strip() if value is not None else None

    @field_validator("owner_email")
    @classmethod
    def validate_updated_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value

    @model_validator(mode="after")
    def require_decision_update(self) -> "RenewalDecisionUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self


class RenewalDecisionReviewRequest(BaseModel):
    approved: bool
    comment: str | None = Field(default=None, max_length=2000)


class RenewalDecisionMilestoneRequest(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    due_date: date | None = None
    owner_email: str | None = Field(default=None, max_length=320)

    @field_validator("title")
    @classmethod
    def validate_milestone_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Milestone title cannot be blank")
        return value

    @field_validator("owner_email")
    @classmethod
    def validate_milestone_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value


class RenewalDecisionMilestoneUpdateRequest(BaseModel):
    completed: bool
    comment: str | None = Field(default=None, max_length=2000)


class RenewalDecisionQuoteRequest(BaseModel):
    supplier: str | None = Field(default=None, max_length=250)
    amount: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    quote_date: date | None = None
    valid_until: date | None = None
    reference: str | None = Field(default=None, max_length=250)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_quote_dates(self) -> "RenewalDecisionQuoteRequest":
        if self.quote_date and self.valid_until and self.valid_until < self.quote_date:
            raise ValueError("valid_until cannot precede quote_date")
        return self


class SupplierCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Supplier name cannot be blank")
        return value.strip()


class SupplierRiskUpdateRequest(BaseModel):
    risk_level: Literal["low", "medium", "high", "critical"]


class TaskAssignmentRequest(BaseModel):
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)

    @field_validator("owner_email")
    @classmethod
    def validate_owner_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        return value


class ObligationCreateRequest(ContractObligation):
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str = Field(min_length=3, max_length=320)

    @field_validator("owner_email")
    @classmethod
    def validate_owner_email(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("owner_email must be a valid email address")
        return value


class ObligationScheduleUpdateRequest(BaseModel):
    status: Literal["active", "paused", "ended"] | None = None
    recurrence_interval_months: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def require_update(self) -> "ObligationScheduleUpdateRequest":
        if self.status is None and self.recurrence_interval_months is None:
            raise ValueError("Provide a schedule status or recurrence interval")
        return self


class ObligationRescheduleRequest(BaseModel):
    due_date: date


class RenewalForecastRequest(BaseModel):
    start_date: date
    end_date: date
    renewal_increase_pct: Decimal = Field(default=Decimal("0"), ge=-100, le=1000, max_digits=6, decimal_places=2)
    renegotiation_savings_pct: Decimal = Field(default=Decimal("10"), ge=0, le=100, max_digits=5, decimal_places=2)

    @model_validator(mode="after")
    def validate_period(self) -> "RenewalForecastRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 366 * 5:
            raise ValueError("forecast periods cannot exceed five years")
        return self


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


class ObligationEvidenceRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["application/pdf", "image/png", "image/jpeg", "text/plain", "application/octet-stream"]
    content_base64: str = Field(min_length=4, max_length=7_000_000)
    comment: str | None = Field(default=None, max_length=2000)


class ObligationComplianceRequest(BaseModel):
    outcome: Literal["failed", "missed"]
    comment: str = Field(min_length=1, max_length=2000)


class ObligationEvidenceReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = Field(default=None, max_length=2000)


class ApprovalPlaybookRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    min_contract_value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    supplier_risk_levels: list[Literal["low", "medium", "high", "critical"]] = Field(default_factory=list, max_length=4)
    changed_clause_categories: list[Literal["effective_term", "expiration", "renewal", "auto_renewal", "notice_period", "termination", "pricing", "data_retention", "liability", "security", "other"]] = Field(default_factory=list, max_length=20)
    required_approvers: list[str] = Field(min_length=1, max_length=20)
    checklist: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("required_approvers")
    @classmethod
    def validate_approvers(cls, value: list[str]) -> list[str]:
        cleaned = sorted({email.strip().casefold() for email in value})
        if any("@" not in email or email.startswith("@") or email.endswith("@") for email in cleaned):
            raise ValueError("required_approvers must contain valid email addresses")
        return cleaned

    @field_validator("checklist", "changed_clause_categories")
    @classmethod
    def validate_playbook_lists(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 250 for item in value):
            raise ValueError("playbook entries must be non-empty and at most 250 characters")
        return [item.strip() for item in value]

    @model_validator(mode="after")
    def validate_threshold_currency(self) -> "ApprovalPlaybookRequest":
        if self.min_contract_value is not None and not self.currency:
            raise ValueError("currency is required with min_contract_value")
        return self


class BulkPortfolioRequest(BaseModel):
    action: Literal["assign_tasks", "transition_tasks", "assign_supplier", "assign_obligations", "review_obligations"]
    ids: list[str] = Field(min_length=1, max_length=100)
    owner_name: str | None = Field(default=None, max_length=150)
    owner_email: str | None = Field(default=None, max_length=320)
    workflow_state: Literal["review", "needs_changes", "pending_approval", "notice_in_progress"] | None = None
    supplier_id: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)
    decision: Literal["approved", "rejected"] | None = None

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 100 for item in value):
            raise ValueError("IDs must be non-empty and at most 100 characters")
        if len(set(value)) != len(value):
            raise ValueError("IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_action_values(self) -> "BulkPortfolioRequest":
        if self.action == "assign_tasks" and self.owner_email is None:
            raise ValueError("owner_email is required to bulk assign tasks")
        if self.owner_email and ("@" not in self.owner_email or self.owner_email.startswith("@") or self.owner_email.endswith("@")):
            raise ValueError("owner_email must be a valid email address")
        if self.action == "transition_tasks" and self.workflow_state is None:
            raise ValueError("workflow_state is required to bulk transition tasks")
        if self.action == "assign_supplier" and self.supplier_id is None:
            raise ValueError("supplier_id is required to bulk assign contracts")
        if self.action == "assign_obligations" and self.owner_email is None:
            raise ValueError("owner_email is required to bulk assign obligations")
        if self.action == "review_obligations" and self.decision is None:
            raise ValueError("decision is required to bulk review obligation evidence")
        return self


class ProviderEvaluationScheduleRequest(BaseModel):
    enabled: bool = True
    interval_hours: int = Field(default=24, ge=1, le=720)
    run_on_provider_change: bool = True
    alert_emails: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("alert_emails")
    @classmethod
    def validate_alert_emails(cls, value: list[str]) -> list[str]:
        emails = [email.strip() for email in value]
        if any(not email or "@" not in email or email.startswith("@") or email.endswith("@") for email in emails):
            raise ValueError("alert_emails must contain valid email addresses")
        if len({email.casefold() for email in emails}) != len(emails):
            raise ValueError("alert_emails must be unique")
        return emails


class PortfolioFilterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dataset: Literal["contracts", "tasks", "suppliers", "obligations"]
    filters: dict[str, str | bool] = Field(default_factory=dict, max_length=10)

    @field_validator("name")
    @classmethod
    def clean_filter_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("filters")
    @classmethod
    def validate_filter_keys(cls, value: dict[str, str | bool]) -> dict[str, str | bool]:
        allowed = {
            "contracts": {"status", "supplier_id", "owner_email", "auto_renew"},
            "tasks": {"status", "workflow_state", "task_kind", "owner_email", "unassigned"},
            "suppliers": {"risk_level"},
            "obligations": {"status", "category", "owner_email", "schedule_status"},
        }
        if any(key not in set().union(*allowed.values()) for key in value):
            raise ValueError("Unsupported portfolio filter")
        return value


class PlaybookApplyRequest(BaseModel):
    changed_clause_categories: list[Literal["effective_term", "expiration", "renewal", "auto_renewal", "notice_period", "termination", "pricing", "data_retention", "liability", "security", "other"]] = Field(default_factory=list, max_length=20)

    @field_validator("changed_clause_categories")
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class PlaybookApprovalRequest(BaseModel):
    approved: bool


class PlaybookChecklistRequest(BaseModel):
    index: int = Field(ge=0, le=100)
    completed: bool
